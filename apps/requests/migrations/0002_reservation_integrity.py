from django.db import migrations

FORWARD = """
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.inventory_stockbalance WHERE reserved<>0)
 OR EXISTS(SELECT 1 FROM public.inventory_stockentry WHERE kind IN ('RESERVE','RELEASE')) THEN
  RAISE EXCEPTION 'Existing reservations require explicit reconciliation before M4';
 END IF;
END; $$;
CREATE SEQUENCE public.request_reference_seq;

CREATE FUNCTION public.reserve_request_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE op uuid; r public.requests_internalrequest%ROWTYPE; p public.catalog_product%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Request history cannot be deleted' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
  IF TG_TABLE_NAME='requests_internalrequest' THEN
   IF ROW(NEW.id,NEW.reference,NEW.assistant_id,NEW.assistant_name,NEW.floor_id,NEW.floor_code,NEW.floor_name,NEW.creation_operation_id)
      IS DISTINCT FROM ROW(OLD.id,OLD.reference,OLD.assistant_id,OLD.assistant_name,OLD.floor_id,OLD.floor_code,OLD.floor_name,OLD.creation_operation_id) THEN
    RAISE EXCEPTION 'Request origin is immutable' USING ERRCODE='23514'; END IF;
   IF OLD.first_delivery_operation_id IS NOT NULL AND NEW.first_delivery_operation_id IS DISTINCT FROM OLD.first_delivery_operation_id THEN
    RAISE EXCEPTION 'First delivery anchor is immutable' USING ERRCODE='23514'; END IF;
  ELSIF TG_TABLE_NAME='requests_requestline' THEN
   IF ROW(NEW.id,NEW.request_id,NEW.product_id,NEW.quantity,NEW.product_code,NEW.product_name,NEW.unit,NEW.maximum,NEW.permission_version,NEW.product_version,NEW.photo_id)
      IS DISTINCT FROM ROW(OLD.id,OLD.request_id,OLD.product_id,OLD.quantity,OLD.product_code,OLD.product_name,OLD.unit,OLD.maximum,OLD.permission_version,OLD.product_version,OLD.photo_id) THEN
    RAISE EXCEPTION 'Request line origin is immutable' USING ERRCODE='23514'; END IF;
  ELSIF TG_TABLE_NAME='requests_reservation' THEN
   IF NEW.line_id IS DISTINCT FROM OLD.line_id OR NEW.product_id IS DISTINCT FROM OLD.product_id THEN
    RAISE EXCEPTION 'Reservation identity is immutable' USING ERRCODE='23514'; END IF;
  END IF;
  RETURN NEW;
 END IF;
 IF TG_TABLE_NAME='requests_internalrequest' THEN
  op:=NEW.creation_operation_id;
  IF NOT EXISTS(SELECT 1 FROM public.accounts_user u JOIN public.catalog_floor f ON f.id=u.floor_id
    WHERE u.id=NEW.assistant_id AND u.is_active AND u.role='ASSISTANT' AND f.active AND f.id=NEW.floor_id
      AND u.display_name=NEW.assistant_name AND f.code=NEW.floor_code AND f.name=NEW.floor_name) THEN
   RAISE EXCEPTION 'Invalid active request owner or floor snapshot' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='requests_requestline' THEN
  SELECT * INTO r FROM public.requests_internalrequest WHERE id=NEW.request_id; op:=r.creation_operation_id;
  SELECT * INTO p FROM public.catalog_product WHERE id=NEW.product_id;
  IF NOT EXISTS(SELECT 1 FROM public.catalog_floorproduct fp WHERE fp.floor_id=r.floor_id AND fp.product_id=p.id
     AND fp.visible AND fp.demandable AND fp.maximum>=NEW.quantity AND fp.maximum=NEW.maximum AND p.version=NEW.permission_version)
   OR p.status<>'ACTIVE' OR p.counted_at IS NULL OR p.code<>NEW.product_code OR p.name<>NEW.product_name OR p.unit<>NEW.unit
   OR p.version<>NEW.product_version OR p.current_photo_id IS DISTINCT FROM NEW.photo_id
   OR EXISTS(SELECT 1 FROM public.cases_stockhold WHERE product_id=p.id) THEN
   RAISE EXCEPTION 'Invalid request product permission or snapshot' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='requests_reservation' THEN
  SELECT q.* INTO r FROM public.requests_internalrequest q JOIN public.requests_requestline l ON l.request_id=q.id WHERE l.id=NEW.line_id;
  op:=r.creation_operation_id;
 ELSIF TG_TABLE_NAME='requests_requestevent' THEN
  op:=NEW.operation_id;
 ELSE
  SELECT operation_id INTO op FROM public.requests_requestevent WHERE id=NEW.event_id;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id
   WHERE o.id=op AND i.state='READY' AND o.actor_role='ASSISTANT' AND o.command_type IN ('requests.create','requests.cancel')) THEN
  RAISE EXCEPTION 'Request facts require a new authorized confirmation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER request_guard BEFORE INSERT OR UPDATE OR DELETE ON public.requests_internalrequest FOR EACH ROW EXECUTE FUNCTION public.reserve_request_guard();
CREATE TRIGGER request_line_guard BEFORE INSERT OR UPDATE OR DELETE ON public.requests_requestline FOR EACH ROW EXECUTE FUNCTION public.reserve_request_guard();
CREATE TRIGGER reservation_guard BEFORE INSERT OR UPDATE OR DELETE ON public.requests_reservation FOR EACH ROW EXECUTE FUNCTION public.reserve_request_guard();
CREATE TRIGGER request_event_guard BEFORE INSERT ON public.requests_requestevent FOR EACH ROW EXECUTE FUNCTION public.reserve_request_guard();
CREATE TRIGGER reservation_change_guard BEFORE INSERT ON public.requests_reservationchange FOR EACH ROW EXECUTE FUNCTION public.reserve_request_guard();
CREATE TRIGGER request_event_immutable BEFORE UPDATE OR DELETE ON public.requests_requestevent FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
CREATE TRIGGER reservation_change_immutable BEFORE UPDATE OR DELETE ON public.requests_reservationchange FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();

CREATE FUNCTION public.reserve_assert_reservations(pid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE total bigint; actual bigint;
BEGIN
 SELECT COALESCE(sum(remaining),0) INTO total FROM public.requests_reservation WHERE product_id=pid;
 SELECT reserved INTO actual FROM public.inventory_stockbalance WHERE product_id=pid;
 IF actual IS DISTINCT FROM total THEN
  RAISE EXCEPTION 'Stock reserved must equal open request reservations' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.inventory_stockentry e WHERE e.product_id=pid AND e.kind IN ('RESERVE','RELEASE') AND NOT EXISTS(
  SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestline l ON l.id=c.line_id
   JOIN public.requests_requestevent a ON a.id=c.event_id WHERE c.entry_id=e.id AND l.product_id=e.product_id AND l.request_id=a.request_id AND e.operation_id=a.operation_id)) THEN
  RAISE EXCEPTION 'Reservation stock entry requires request origin' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_assert_request(rid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE r public.requests_internalrequest%ROWTYPE; cancels bigint;
BEGIN
 SELECT * INTO r FROM public.requests_internalrequest WHERE id=rid; IF NOT FOUND THEN RETURN; END IF;
 SELECT count(*) INTO cancels FROM public.requests_requestevent WHERE request_id=rid AND kind='CANCELLED';
 IF NOT EXISTS(SELECT 1 FROM public.requests_requestevent WHERE request_id=rid AND kind='CREATED' AND operation_id=r.creation_operation_id)
 OR NOT EXISTS(SELECT 1 FROM public.requests_requestline WHERE request_id=rid)
 OR r.version<>cancels OR r.status<>(CASE WHEN cancels=1 THEN 'CANCELLED' ELSE 'NEW' END) THEN
  RAISE EXCEPTION 'Invalid request state or missing creation fact' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestline l LEFT JOIN public.requests_reservation v ON v.line_id=l.id
  WHERE l.request_id=rid AND (v.line_id IS NULL OR v.product_id<>l.product_id OR l.quantity<>l.delivered+l.closed+v.remaining
    OR v.version<>cancels OR l.closed<>CASE WHEN cancels=1 THEN l.quantity ELSE 0 END
    OR NOT EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent a ON a.id=c.event_id
      JOIN public.inventory_stockentry e ON e.id=c.entry_id WHERE c.line_id=l.id AND a.request_id=rid AND a.kind='CREATED' AND e.delta_r=l.quantity)
    OR (cancels=1 AND NOT EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent a ON a.id=c.event_id
      JOIN public.inventory_stockentry e ON e.id=c.entry_id WHERE c.line_id=l.id AND a.request_id=rid AND a.kind='CANCELLED' AND e.delta_r=-l.quantity)))) THEN
  RAISE EXCEPTION 'Q must equal L+C+U and reservation facts must match projections' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestevent a JOIN public.core_operation o ON o.id=a.operation_id JOIN public.core_operationintent i ON i.id=o.intent_id
   WHERE a.request_id=rid AND (i.state<>'CONFIRMED' OR o.actor_id<>r.assistant_id OR o.actor_role<>'ASSISTANT'
    OR (a.kind='CREATED' AND (o.command_type<>'requests.create' OR a.operation_id<>r.creation_operation_id))
    OR (a.kind='CANCELLED' AND (o.command_type<>'requests.cancel' OR o.resource_id IS DISTINCT FROM r.id))
    OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=o.id AND b.aggregate_id=r.id
       AND b.event_type=CASE WHEN a.kind='CREATED' THEN 'request.created' ELSE 'request.cancelled' END))) THEN
  RAISE EXCEPTION 'Request events require confirmed matching operations and outbox events' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent a ON a.id=c.event_id
    JOIN public.requests_requestline l ON l.id=c.line_id JOIN public.inventory_stockentry e ON e.id=c.entry_id
    WHERE (a.request_id=rid OR l.request_id=rid) AND (a.request_id<>l.request_id OR e.operation_id<>a.operation_id OR e.product_id<>l.product_id
      OR e.delta_p<>0 OR e.unit<>l.unit OR (a.kind='CREATED' AND (e.kind<>'RESERVE' OR e.delta_r<>l.quantity))
      OR (a.kind='CANCELLED' AND (e.kind<>'RELEASE' OR e.delta_r<>-l.quantity)))) THEN
  RAISE EXCEPTION 'Invalid typed request reservation source' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_request_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE rid uuid; pid uuid;
BEGIN
 IF TG_TABLE_NAME IN ('inventory_stockbalance','inventory_stockentry') THEN pid:=NEW.product_id;
 ELSIF TG_TABLE_NAME='requests_internalrequest' THEN rid:=NEW.id;
 ELSIF TG_TABLE_NAME IN ('requests_requestline','requests_requestevent') THEN rid:=NEW.request_id;
 ELSE
  SELECT request_id,product_id INTO rid,pid FROM public.requests_requestline WHERE id=NEW.line_id;
 END IF;
 IF rid IS NOT NULL THEN PERFORM public.reserve_assert_request(rid); END IF;
 IF pid IS NOT NULL THEN PERFORM public.reserve_assert_reservations(pid); END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER request_consistency AFTER INSERT OR UPDATE ON public.requests_internalrequest DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_request_consistency();
CREATE CONSTRAINT TRIGGER request_line_consistency AFTER INSERT OR UPDATE ON public.requests_requestline DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_request_consistency();
CREATE CONSTRAINT TRIGGER reservation_consistency AFTER INSERT OR UPDATE ON public.requests_reservation DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_request_consistency();
CREATE CONSTRAINT TRIGGER request_event_consistency AFTER INSERT ON public.requests_requestevent DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_request_consistency();
CREATE CONSTRAINT TRIGGER reservation_change_consistency AFTER INSERT ON public.requests_reservationchange DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_request_consistency();
CREATE CONSTRAINT TRIGGER stock_request_balance_consistency AFTER INSERT OR UPDATE ON public.inventory_stockbalance DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_request_consistency();
CREATE CONSTRAINT TRIGGER stock_request_entry_consistency AFTER INSERT ON public.inventory_stockentry DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_request_consistency();
"""
REVERSE = """
DROP TRIGGER stock_request_entry_consistency ON public.inventory_stockentry;
DROP TRIGGER stock_request_balance_consistency ON public.inventory_stockbalance;
DROP TRIGGER reservation_change_consistency ON public.requests_reservationchange;
DROP TRIGGER request_event_consistency ON public.requests_requestevent;
DROP TRIGGER reservation_consistency ON public.requests_reservation;
DROP TRIGGER request_line_consistency ON public.requests_requestline;
DROP TRIGGER request_consistency ON public.requests_internalrequest;
DROP FUNCTION public.reserve_request_consistency();
DROP FUNCTION public.reserve_assert_request(uuid);
DROP FUNCTION public.reserve_assert_reservations(uuid);
DROP TRIGGER reservation_change_immutable ON public.requests_reservationchange;
DROP TRIGGER request_event_immutable ON public.requests_requestevent;
DROP TRIGGER reservation_change_guard ON public.requests_reservationchange;
DROP TRIGGER request_event_guard ON public.requests_requestevent;
DROP TRIGGER reservation_guard ON public.requests_reservation;
DROP TRIGGER request_line_guard ON public.requests_requestline;
DROP TRIGGER request_guard ON public.requests_internalrequest;
DROP FUNCTION public.reserve_request_guard();
DROP SEQUENCE public.request_reference_seq;
"""


class Migration(migrations.Migration):
    dependencies = [("requests", "0001_initial"), ("inventory", "0003_trigger_record_types")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
