from importlib import import_module
from django.db import migrations

base = import_module("apps.requests.migrations.0002_reservation_integrity")
guard = base.FORWARD.split("CREATE FUNCTION public.reserve_request_guard()", 1)[1].split(
    "CREATE TRIGGER request_guard", 1
)[0]
guard = "CREATE OR REPLACE FUNCTION public.reserve_request_guard()" + guard
guard = guard.replace(
    "AND o.actor_role='ASSISTANT' AND o.command_type IN ('requests.create','requests.cancel')",
    "AND ((o.actor_role='ASSISTANT' AND o.command_type IN ('requests.create','requests.cancel')) OR (o.actor_role='REPLENISHER' AND o.command_type IN ('requests.claim','requests.deliver')))",
)
SQL = r"""
CREATE OR REPLACE FUNCTION public.reserve_assert_request(rid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE r public.requests_internalrequest%ROWTYPE; n bigint; v bigint; cancels bigint; claims bigint; nd bigint; expected text; anchor uuid;
BEGIN
 SELECT * INTO r FROM public.requests_internalrequest WHERE id=rid; IF NOT FOUND THEN RETURN; END IF;
 SELECT count(*),max(revision),count(*) FILTER(WHERE kind='CANCELLED'),count(*) FILTER(WHERE kind='CLAIMED') INTO n,v,cancels,claims FROM public.requests_requestevent WHERE request_id=rid;
 SELECT count(*) INTO nd FROM public.requests_delivery WHERE request_id=rid;
 IF n<>v+1 OR r.version<>v OR cancels>1 OR claims>1 OR NOT EXISTS(SELECT 1 FROM public.requests_requestevent WHERE request_id=rid AND kind='CREATED' AND revision=0 AND operation_id=r.creation_operation_id)
 OR NOT EXISTS(SELECT 1 FROM public.requests_requestline WHERE request_id=rid) THEN
  RAISE EXCEPTION 'Request event revisions must form a complete history' USING ERRCODE='23514'; END IF;
 IF (claims=0 AND r.assigned_to_id IS NOT NULL) OR (claims=1 AND NOT EXISTS(SELECT 1 FROM public.requests_requestevent e JOIN public.core_operation o ON o.id=e.operation_id WHERE e.request_id=rid AND e.kind='CLAIMED' AND e.revision=1 AND o.actor_id=r.assigned_to_id)) THEN
  RAISE EXCEPTION 'Assignment requires its unique immutable claim' USING ERRCODE='23514'; END IF;
 IF cancels=1 THEN
  IF nd<>0 OR r.first_delivery_operation_id IS NOT NULL THEN RAISE EXCEPTION 'Cannot cancel any confirmed delivery' USING ERRCODE='23514'; END IF;
  expected:='CANCELLED';
 ELSIF nd=0 THEN expected:=CASE WHEN claims=1 THEN 'PREPARING' ELSE 'NEW' END;
 ELSE
  IF claims<>1 THEN RAISE EXCEPTION 'Delivery requires assignment' USING ERRCODE='23514'; END IF;
  SELECT d.operation_id INTO anchor FROM public.requests_delivery d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.request_id=rid ORDER BY e.revision LIMIT 1;
  IF r.first_delivery_operation_id IS DISTINCT FROM anchor THEN RAISE EXCEPTION 'Invalid first delivery anchor' USING ERRCODE='23514'; END IF;
  expected:=CASE WHEN EXISTS(SELECT 1 FROM public.requests_requestline l JOIN public.requests_reservation z ON z.line_id=l.id WHERE l.request_id=rid AND z.remaining>0) THEN 'PARTIAL_OPEN'
   WHEN EXISTS(SELECT 1 FROM public.requests_requestline WHERE request_id=rid AND closed>0) THEN 'PARTIAL_CLOSED' ELSE 'DELIVERED' END;
 END IF;
 IF r.status<>expected OR r.closure_reason<>(CASE WHEN expected='PARTIAL_CLOSED' THEN 'SHORTAGE' ELSE '' END) OR (nd=0 AND r.first_delivery_operation_id IS NOT NULL) THEN
  RAISE EXCEPTION 'Request state must match immutable facts' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestline l LEFT JOIN public.requests_reservation z ON z.line_id=l.id WHERE l.request_id=rid AND (
  z.line_id IS NULL OR z.product_id<>l.product_id OR l.quantity<>l.delivered+l.closed+z.remaining
  OR l.delivered<>COALESCE((SELECT sum(handed) FROM public.requests_deliveryline dl WHERE dl.line_id=l.id),0)
  OR l.closed<>CASE WHEN cancels=1 THEN l.quantity ELSE COALESCE((SELECT sum(missing) FROM public.requests_deliveryline dl WHERE dl.line_id=l.id),0) END
  OR z.remaining<>COALESCE((SELECT sum(e.delta_r) FROM public.requests_reservationchange c JOIN public.inventory_stockentry e ON e.id=c.entry_id WHERE c.line_id=l.id),0)
  OR z.version<>(SELECT count(DISTINCT c.event_id)-1 FROM public.requests_reservationchange c WHERE c.line_id=l.id)
  OR NOT EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent e ON e.id=c.event_id JOIN public.inventory_stockentry s ON s.id=c.entry_id WHERE c.line_id=l.id AND e.kind='CREATED' AND s.delta_r=l.quantity)
 )) THEN RAISE EXCEPTION 'Q=L+C+U and line projections must match delivery and reservation facts' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestevent e JOIN public.core_operation o ON o.id=e.operation_id JOIN public.core_operationintent i ON i.id=o.intent_id WHERE e.request_id=rid AND (
  i.state<>'CONFIRMED'
  OR (e.kind IN ('CREATED','CANCELLED') AND (o.actor_id<>r.assistant_id OR o.actor_role<>'ASSISTANT'))
  OR (e.kind IN ('CLAIMED','DELIVERED') AND (o.actor_id<>r.assigned_to_id OR o.actor_role<>'REPLENISHER'))
  OR o.command_type<>CASE e.kind WHEN 'CREATED' THEN 'requests.create' WHEN 'CANCELLED' THEN 'requests.cancel' WHEN 'CLAIMED' THEN 'requests.claim' ELSE 'requests.deliver' END
  OR (e.kind<>'CREATED' AND o.resource_id IS DISTINCT FROM rid)
  OR (e.kind='CREATED' AND e.revision<>0)
  OR (e.kind='CANCELLED' AND e.revision<>r.version)
  OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=o.id AND b.aggregate_id=rid AND b.event_type=CASE e.kind WHEN 'CREATED' THEN 'request.created' WHEN 'CANCELLED' THEN 'request.cancelled' WHEN 'CLAIMED' THEN 'request.claimed' ELSE 'request.delivered' END)
  OR (e.kind='DELIVERED' AND NOT EXISTS(SELECT 1 FROM public.requests_delivery d WHERE d.event_id=e.id AND d.operation_id=e.operation_id AND d.request_id=rid))
 )) THEN RAISE EXCEPTION 'Request event requires matching confirmed operation and outbox' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent a ON a.id=c.event_id JOIN public.requests_requestline l ON l.id=c.line_id JOIN public.inventory_stockentry e ON e.id=c.entry_id
  WHERE (a.request_id=rid OR l.request_id=rid) AND (a.request_id<>l.request_id OR e.operation_id<>a.operation_id OR e.product_id<>l.product_id OR e.unit<>l.unit
  OR (a.kind='CREATED' AND (e.kind<>'RESERVE' OR e.delta_p<>0 OR e.delta_r<>l.quantity))
  OR (a.kind='CANCELLED' AND (e.kind<>'RELEASE' OR e.delta_p<>0 OR e.delta_r<>-l.quantity))
  OR a.kind='CLAIMED'
  OR (a.kind='DELIVERED' AND NOT EXISTS(SELECT 1 FROM public.requests_delivery d JOIN public.requests_deliveryline dl ON dl.delivery_id=d.id WHERE d.event_id=a.id AND dl.line_id=l.id AND (
    (e.kind='DELIVER' AND dl.handed>0 AND e.delta_p=-dl.handed AND e.delta_r=-dl.handed)
    OR (e.kind='RELEASE' AND dl.missing>0 AND e.delta_p=0 AND e.delta_r=-dl.missing)
    OR (e.kind='SHORTAGE' AND dl.missing>0 AND e.delta_p=-dl.missing AND e.delta_r=0)) ))
 )) THEN RAISE EXCEPTION 'Invalid typed delivery/reservation entry' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_delivery d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.request_id=rid AND (
   d.operation_id<>e.operation_id OR e.kind<>'DELIVERED' OR e.request_id<>rid OR e.revision<2 OR d.operator_id<>r.assigned_to_id
   OR NOT EXISTS(SELECT 1 FROM public.core_operation o WHERE o.id=d.operation_id AND o.actor_id=d.operator_id)
   OR (SELECT count(*) FROM public.requests_deliveryline WHERE delivery_id=d.id)<>(SELECT count(*) FROM public.requests_requestline WHERE request_id=rid)
   OR (SELECT COALESCE(sum(handed),0) FROM public.requests_deliveryline WHERE delivery_id=d.id)=0 AND (d.mode<>'SHORTAGE' OR (SELECT COALESCE(sum(missing),0) FROM public.requests_deliveryline WHERE delivery_id=d.id)=0)
   OR (d.mode='SHORTAGE' AND NOT EXISTS(SELECT 1 FROM public.requests_deliveryissue x WHERE x.delivery_id=d.id AND x.kind='SHORTAGE' AND x.operation_id=d.operation_id))
 )) THEN RAISE EXCEPTION 'Invalid immutable delivery fact' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_deliveryline dl JOIN public.requests_delivery d ON d.id=dl.delivery_id JOIN public.requests_requestevent a ON a.id=d.event_id JOIN public.requests_requestline l ON l.id=dl.line_id WHERE d.request_id=rid AND (
  l.request_id<>rid OR dl.remaining_before<>l.quantity-COALESCE((SELECT sum(prev.handed+prev.missing) FROM public.requests_deliveryline prev JOIN public.requests_delivery pd ON pd.id=prev.delivery_id JOIN public.requests_requestevent pe ON pe.id=pd.event_id WHERE prev.line_id=l.id AND pe.revision<a.revision),0)
  OR (d.mode='COMPLETE' AND (dl.handed<>dl.remaining_before OR dl.missing<>0))
  OR (d.mode='LATER' AND dl.missing<>0)
  OR (d.mode='SHORTAGE' AND dl.handed+dl.missing<>dl.remaining_before)
  OR (dl.handed>0 AND (SELECT count(*) FROM public.requests_reservationchange c JOIN public.inventory_stockentry s ON s.id=c.entry_id WHERE c.line_id=l.id AND c.event_id=a.id AND s.kind='DELIVER')<>1)
  OR (dl.missing>0 AND ((SELECT count(*) FROM public.requests_reservationchange c JOIN public.inventory_stockentry s ON s.id=c.entry_id WHERE c.line_id=l.id AND c.event_id=a.id AND s.kind='RELEASE')<>1 OR (SELECT count(*) FROM public.requests_reservationchange c JOIN public.inventory_stockentry s ON s.id=c.entry_id WHERE c.line_id=l.id AND c.event_id=a.id AND s.kind='SHORTAGE')<>1))
 )) THEN RAISE EXCEPTION 'Delivery amounts require exact earlier remainder and unique ledger links' USING ERRCODE='23514'; END IF;
END; $$;

CREATE OR REPLACE FUNCTION public.reserve_assert_reservations(pid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF (SELECT reserved FROM public.inventory_stockbalance WHERE product_id=pid) IS DISTINCT FROM (SELECT COALESCE(sum(remaining),0) FROM public.requests_reservation WHERE product_id=pid) THEN RAISE EXCEPTION 'Stock reserved must equal open reservations' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.inventory_stockentry e WHERE e.product_id=pid AND e.kind IN ('RESERVE','RELEASE','DELIVER','SHORTAGE') AND NOT EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestline l ON l.id=c.line_id JOIN public.requests_requestevent a ON a.id=c.event_id WHERE c.entry_id=e.id AND l.product_id=pid AND l.request_id=a.request_id AND e.operation_id=a.operation_id)) THEN RAISE EXCEPTION 'Reservation/delivery stock entry requires typed request origin' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_delivery_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE op uuid; did uuid; d public.requests_delivery%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='requests_delivery' THEN op:=NEW.operation_id; did:=NEW.id;
 ELSIF TG_TABLE_NAME='requests_deliveryline' THEN did:=NEW.delivery_id; SELECT operation_id INTO op FROM public.requests_delivery WHERE id=did;
 ELSE did:=NEW.delivery_id; op:=NEW.operation_id; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id WHERE o.id=op AND i.state='READY' AND o.actor_role='REPLENISHER' AND o.command_type IN ('requests.deliver','deliveries.report_error')) THEN RAISE EXCEPTION 'Delivery facts require new replenisher operation' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='requests_deliveryissue' THEN
 IF NEW.kind='DELIVERY_ERROR' THEN
  SELECT * INTO d FROM public.requests_delivery WHERE id=did;
  IF NOT EXISTS(SELECT 1 FROM public.core_operation WHERE id=op AND actor_id=d.operator_id AND command_type='deliveries.report_error' AND resource_id=d.id) THEN RAISE EXCEPTION 'Only original operator can report this delivery' USING ERRCODE='23514'; END IF;
 END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE FUNCTION public.reserve_delivery_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE rid uuid; did uuid;
BEGIN
 IF TG_TABLE_NAME='requests_delivery' THEN did:=NEW.id; ELSE did:=NEW.delivery_id; END IF;
 SELECT request_id INTO rid FROM public.requests_delivery WHERE id=did;
 PERFORM public.reserve_assert_request(rid);
 IF TG_TABLE_NAME='requests_deliveryissue' THEN
  IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.requests_delivery d ON d.id=NEW.delivery_id WHERE o.id=NEW.operation_id AND i.state='CONFIRMED' AND (
   (NEW.kind='SHORTAGE' AND o.id=d.operation_id AND d.mode='SHORTAGE') OR (NEW.kind='DELIVERY_ERROR' AND o.command_type='deliveries.report_error' AND o.actor_id=d.operator_id))
   AND EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=o.id AND b.aggregate_id=NEW.id AND b.event_type=CASE WHEN NEW.kind='SHORTAGE' THEN 'delivery.shortage.reported' ELSE 'delivery.error.reported' END)) THEN RAISE EXCEPTION 'Delivery issue requires matching confirmed operation and event' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END; $$;
"""
for table in ["requests_delivery", "requests_deliveryline", "requests_deliveryissue"]:
    SQL += f"CREATE TRIGGER m5_guard BEFORE INSERT ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.reserve_delivery_guard();\nCREATE TRIGGER m5_immutable BEFORE UPDATE OR DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();\nCREATE CONSTRAINT TRIGGER m5_consistency AFTER INSERT ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_delivery_consistency();\n"


class Migration(migrations.Migration):
    dependencies = [
        ("requests", "0004_delivery_deliveryissue_deliveryline_and_more"),
        ("inventory", "0004_remove_stockentry_stock_entry_delta_p_and_more"),
    ]
    operations = [migrations.RunSQL(guard + SQL)]
