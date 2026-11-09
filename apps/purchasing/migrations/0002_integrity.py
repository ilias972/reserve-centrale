"""Faits M7 immuables, achats distincts sérialisés et projections vérifiées à COMMIT."""

from django.db import migrations

SQL = r"""
CREATE SEQUENCE public.purchase_reference_seq;
CREATE FUNCTION public.reserve_m7_operation(oid uuid, kind text, rid uuid) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.audit_auditevent a ON a.operation_id=o.id
 WHERE o.id=oid AND o.command_type=kind AND o.actor_role='ADMIN' AND o.resource_id IS NOT DISTINCT FROM rid AND i.state='CONFIRMED');
$$;
CREATE FUNCTION public.reserve_m7_order_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE s public.catalog_supplier%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Purchase history cannot be deleted' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
  IF (to_jsonb(NEW)-ARRAY['status','version']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['status','version']) THEN RAISE EXCEPTION 'Initial purchase is immutable' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT * INTO s FROM public.catalog_supplier WHERE id=NEW.supplier_id FOR UPDATE;
  IF NEW.supplier_code<>s.code OR NEW.supplier_name<>s.name OR NEW.supplier_website<>s.website
  OR NEW.reference_key<>normalize(btrim(NEW.external_reference,' '),NFC)
  OR NEW.reference!~'^ACH-[0-9]{8,}$' OR NEW.purchased_on>(CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Paris')::date
  OR NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.accounts_user u ON u.id=o.actor_id WHERE o.id=NEW.creation_operation_id AND o.command_type='orders.create' AND o.resource_id IS NULL AND o.actor_role='ADMIN' AND i.state='READY' AND u.role='ADMIN' AND u.is_active AND NOT u.must_change_password AND NEW.author_name=u.display_name)
  THEN RAISE EXCEPTION 'Invalid purchase origin or snapshot' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER m7_order_guard BEFORE INSERT OR UPDATE OR DELETE ON public.purchasing_purchaseorder FOR EACH ROW EXECUTE FUNCTION public.reserve_m7_order_guard();
CREATE FUNCTION public.reserve_m7_line_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE p public.catalog_product%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Purchase line is immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
  IF (to_jsonb(NEW)-'cancelled') IS DISTINCT FROM (to_jsonb(OLD)-'cancelled') THEN RAISE EXCEPTION 'Initial ordered quantities are immutable' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT * INTO p FROM public.catalog_product WHERE id=NEW.product_id;
  IF NEW.product_code<>p.code OR NEW.product_name<>p.name OR NEW.unit<>p.unit OR NEW.product_version<>p.version OR NEW.photo_id IS DISTINCT FROM p.current_photo_id
  OR NOT EXISTS(SELECT 1 FROM public.purchasing_purchaseorder po JOIN public.core_operation o ON o.id=po.creation_operation_id JOIN public.core_operationintent i ON i.id=o.intent_id WHERE po.id=NEW.order_id AND i.state='READY')
  THEN RAISE EXCEPTION 'Invalid purchase line snapshot or terminal purchase' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER m7_line_guard BEFORE INSERT OR UPDATE OR DELETE ON public.purchasing_orderline FOR EACH ROW EXECUTE FUNCTION public.reserve_m7_line_guard();
CREATE FUNCTION public.reserve_m7_assert_order(oid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE po public.purchasing_purchaseorder%ROWTYPE; n bigint; last_rev bigint; key jsonb; expected text;
BEGIN
 SELECT * INTO po FROM public.purchasing_purchaseorder WHERE id=oid;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing purchase' USING ERRCODE='23514'; END IF;
 SELECT jsonb_agg(jsonb_build_array(product_id::text,quantity) ORDER BY product_id) INTO key FROM public.purchasing_orderline WHERE order_id=oid;
 SELECT count(*),coalesce(max(revision),0) INTO n,last_rev FROM public.purchasing_remainderevent WHERE order_id=oid;
 IF key IS NULL OR po.lines_key<>key OR po.version<>n OR n<>last_rev OR NOT public.reserve_m7_operation(po.creation_operation_id,'orders.create',NULL)
 OR EXISTS(SELECT 1 FROM public.purchasing_orderline l WHERE l.order_id=oid AND (l.received<>0 OR l.cancelled<>coalesce((SELECT sum(c.quantity) FROM public.purchasing_remainderline c WHERE c.line_id=l.id),0)))
 THEN RAISE EXCEPTION 'Purchase quantities, origin or revisions inconsistent' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchasing_orderline WHERE order_id=oid AND quantity-received-cancelled>0) THEN expected='WAITING'; ELSE expected='CANCELLED_NO_RECEIPT'; END IF;
 IF po.status<>expected THEN RAISE EXCEPTION 'Invalid purchase status' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.audit_auditevent a WHERE a.operation_id=po.creation_operation_id AND a.after->>'id'=oid::text AND a.after->>'supplier_name'=po.supplier_name AND a.after->>'purchased_on'=po.purchased_on::text AND a.after->>'external_reference'=po.external_reference)
 OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=po.creation_operation_id AND b.event_type='purchase.recorded' AND b.aggregate_id=oid AND b.revision=1 AND b.payload->'order'->>'id'=oid::text AND b.payload->'recipient_roles'='["ADMIN","ACCOUNTANT_EXTERNAL"]'::jsonb)
 THEN RAISE EXCEPTION 'Purchase audit or future notification missing' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchasing_remainderevent e WHERE e.order_id=oid AND (
  NOT public.reserve_m7_operation(e.operation_id,'orders.cancel_remainder',oid)
  OR NOT EXISTS(SELECT 1 FROM public.purchasing_remainderline c WHERE c.event_id=e.id)
  OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=e.operation_id AND b.event_type='purchase.remainder_cancelled' AND b.aggregate_id=oid AND b.revision=e.revision)
 )) THEN RAISE EXCEPTION 'Invalid remainder event' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchasing_remainderline c JOIN public.purchasing_remainderevent e ON e.id=c.event_id JOIN public.purchasing_orderline l ON l.id=c.line_id WHERE e.order_id=oid AND (
  l.order_id<>oid OR c.cancelled_before<>coalesce((SELECT sum(prior.quantity) FROM public.purchasing_remainderline prior JOIN public.purchasing_remainderevent pe ON pe.id=prior.event_id WHERE prior.line_id=c.line_id AND pe.revision<e.revision),0)
  OR c.cancelled_after>l.quantity
 )) THEN RAISE EXCEPTION 'Invalid remainder line history' USING ERRCODE='23514'; END IF;
END; $$;
CREATE FUNCTION public.reserve_m7_order_check() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE oid uuid;
BEGIN
 IF TG_TABLE_NAME='purchasing_purchaseorder' THEN oid=NEW.id;
 ELSIF TG_TABLE_NAME IN ('purchasing_orderline','purchasing_remainderevent','purchasing_duplicateacknowledgement') THEN oid=NEW.order_id;
 ELSE SELECT order_id INTO oid FROM public.purchasing_remainderevent WHERE id=NEW.event_id;
 END IF;
 PERFORM public.reserve_m7_assert_order(oid); RETURN NULL;
END; $$;
CREATE FUNCTION public.reserve_m7_duplicates_check() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE po public.purchasing_purchaseorder%ROWTYPE; ids jsonb; ack public.purchasing_duplicateacknowledgement%ROWTYPE; target uuid;
BEGIN
 target=CASE WHEN TG_TABLE_NAME='purchasing_purchaseorder' THEN (to_jsonb(NEW)->>'id')::uuid ELSE (to_jsonb(NEW)->>'order_id')::uuid END;
 SELECT * INTO po FROM public.purchasing_purchaseorder WHERE id=target;
 SELECT coalesce(jsonb_agg(id::text ORDER BY id),'[]'::jsonb) INTO ids FROM public.purchasing_purchaseorder o WHERE o.supplier_id=po.supplier_id AND o.id<>po.id AND (
  (po.reference_key<>'' AND o.reference_key=po.reference_key) OR (po.reference_key='' AND o.purchased_on=po.purchased_on AND o.lines_key=po.lines_key));
 SELECT * INTO ack FROM public.purchasing_duplicateacknowledgement WHERE order_id=target;
 IF (ids<>'[]'::jsonb AND (ack.order_id IS NULL OR ack.candidate_ids<>ids OR NOT ack.verified_distinct)) OR (ids='[]'::jsonb AND ack.order_id IS NOT NULL)
 THEN RAISE EXCEPTION 'Duplicate purchase requires explicit candidate verification' USING ERRCODE='23514'; END IF;
 IF ack.order_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.audit_auditevent a WHERE a.operation_id=po.creation_operation_id AND a.details->0->'candidate_ids'=ids AND a.details->0->>'candidate_fingerprint'=ack.candidate_fingerprint AND a.details->0->>'distinct_purchase_verified'='true') THEN RAISE EXCEPTION 'Duplicate acknowledgement must be audited' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER m7_duplicates AFTER INSERT ON public.purchasing_purchaseorder DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m7_duplicates_check();
CREATE CONSTRAINT TRIGGER m7_ack_check AFTER INSERT ON public.purchasing_duplicateacknowledgement DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m7_duplicates_check();
CREATE FUNCTION public.reserve_m7_rec_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE projection public.purchasing_recommendation%ROWTYPE; cnt bigint; last_rev bigint;
BEGIN
 IF TG_TABLE_NAME='purchasing_recommendation' THEN SELECT * INTO projection FROM public.purchasing_recommendation WHERE product_id=NEW.product_id;
 ELSE SELECT * INTO projection FROM public.purchasing_recommendation WHERE product_id=NEW.recommendation_id; END IF;
 SELECT count(*),max(revision) INTO cnt,last_rev FROM public.purchasing_recommendationevent WHERE recommendation_id=projection.product_id;
 IF cnt<1 OR projection.version<>cnt OR cnt<>last_rev OR projection.latest_event_id IS DISTINCT FROM (SELECT id FROM public.purchasing_recommendationevent WHERE recommendation_id=projection.product_id AND revision=last_rev)
 OR EXISTS(SELECT 1 FROM public.purchasing_recommendationevent e WHERE e.recommendation_id=projection.product_id AND (
  NOT public.reserve_m7_operation(e.operation_id,CASE WHEN e.action='DEFER' THEN 'recommendations.defer' ELSE 'recommendations.reactivate' END,projection.product_id)
  OR (e.before->>'version')::bigint IS DISTINCT FROM e.revision-1 OR (e.after->>'version')::bigint IS DISTINCT FROM e.revision
  OR e.before->>'state' IS DISTINCT FROM CASE WHEN e.action='DEFER' THEN 'ACTIVE' ELSE 'DEFERRED' END
  OR e.after->>'state' IS DISTINCT FROM CASE WHEN e.action='DEFER' THEN 'DEFERRED' ELSE 'ACTIVE' END
  OR NOT EXISTS(SELECT 1 FROM public.inventory_stockentry s WHERE s.product_id=e.recommendation_id AND s.version_after=e.stock_version AND s.physical_after-s.reserved_after=e.available)
  OR NOT EXISTS(SELECT 1 FROM public.audit_auditevent a WHERE a.operation_id=e.operation_id AND a.before=e.before AND a.after=e.after)
  OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=e.operation_id AND b.aggregate_id=e.recommendation_id AND b.revision=e.revision AND b.event_type=CASE WHEN e.action='DEFER' THEN 'recommendation.deferred' ELSE 'recommendation.reactivated' END)
 )) THEN RAISE EXCEPTION 'Invalid recommendation history or projection' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
CREATE FUNCTION public.reserve_m7_fact_insert() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE oid uuid; kind text; resource uuid;
BEGIN
 IF TG_TABLE_NAME='purchasing_recommendationevent' THEN oid=NEW.operation_id; resource=NEW.recommendation_id; kind=CASE WHEN NEW.action='DEFER' THEN 'recommendations.defer' ELSE 'recommendations.reactivate' END;
 ELSIF TG_TABLE_NAME='purchasing_remainderevent' THEN oid=NEW.operation_id; resource=NEW.order_id; kind='orders.cancel_remainder';
 ELSIF TG_TABLE_NAME='purchasing_remainderline' THEN SELECT operation_id,order_id INTO oid,resource FROM public.purchasing_remainderevent WHERE id=NEW.event_id; kind='orders.cancel_remainder';
 ELSE SELECT creation_operation_id INTO oid FROM public.purchasing_purchaseorder WHERE id=NEW.order_id; resource=NULL; kind='orders.create'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.accounts_user u ON u.id=o.actor_id WHERE o.id=oid AND o.command_type=kind AND o.resource_id IS NOT DISTINCT FROM resource AND o.actor_role='ADMIN' AND u.role='ADMIN' AND u.is_active AND NOT u.must_change_password AND i.state='READY') THEN RAISE EXCEPTION 'Administrative fact requires a live operation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
"""
for table in [
    "purchaseorder",
    "orderline",
    "remainderevent",
    "remainderline",
    "duplicateacknowledgement",
]:
    SQL += f"CREATE CONSTRAINT TRIGGER m7_order_check AFTER INSERT OR UPDATE ON public.purchasing_{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m7_order_check();\n"
for table in ["recommendationevent", "remainderevent", "remainderline", "duplicateacknowledgement"]:
    SQL += f"CREATE TRIGGER m7_fact_immutable BEFORE UPDATE OR DELETE ON public.purchasing_{table} FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();\nCREATE TRIGGER m7_fact_insert BEFORE INSERT ON public.purchasing_{table} FOR EACH ROW EXECUTE FUNCTION public.reserve_m7_fact_insert();\n"
for table in ["recommendation", "recommendationevent"]:
    SQL += f"CREATE CONSTRAINT TRIGGER m7_rec_check AFTER INSERT OR UPDATE ON public.purchasing_{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m7_rec_guard();\n"
SQL += "CREATE TRIGGER m7_rec_no_delete BEFORE DELETE ON public.purchasing_recommendation FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();\n"


class Migration(migrations.Migration):
    dependencies = [
        ("purchasing", "0001_initial"),
        ("inventory", "0005_m6_facts"),
        ("requests", "0010_m6_integrity"),
    ]
    operations = [migrations.RunSQL(SQL, reverse_sql=migrations.RunSQL.noop)]
