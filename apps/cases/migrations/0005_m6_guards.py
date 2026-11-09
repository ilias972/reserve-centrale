from importlib import import_module
from django.db import migrations

old = import_module("apps.cases.migrations.0002_guards").FORWARD
consistency = old.split("CREATE FUNCTION public.reserve_case_consistency()", 1)[1].split(
    "CREATE CONSTRAINT TRIGGER case_consistency", 1
)[0]
consistency = "CREATE OR REPLACE FUNCTION public.reserve_case_consistency()" + consistency
consistency = consistency.replace(
    "i.command_type='inventory.adjust'",
    "i.command_type IN ('inventory.adjust','corrections.confirm')",
)
consistency = consistency.replace(
    "h.case_id=cid)) THEN",
    "h.case_id=cid) AND NOT EXISTS(SELECT 1 FROM public.cases_case cs JOIN public.cases_caseevent e ON e.case_id=cs.id JOIN public.cases_stockreconciliation x ON x.case_id=cs.id AND x.operation_id=e.operation_id WHERE cs.stock_case_id=cid AND cs.status='RESOLVED' AND e.kind='STOCK_RECONCILED')) THEN",
)
consistency = consistency.replace(
    "IF TG_TABLE_NAME='cases_stockhold' AND NOT EXISTS(SELECT 1 FROM public.cases_stockcaseproduct WHERE case_id=cid AND product_id=NEW.product_id) THEN",
    "IF TG_TABLE_NAME='cases_stockhold' THEN IF NOT EXISTS(SELECT 1 FROM public.cases_stockcaseproduct WHERE case_id=cid AND product_id=NEW.product_id) THEN",
).replace(
    "'Hold requires matching conflict product' USING ERRCODE='23514';\n END IF;",
    "'Hold requires matching conflict product' USING ERRCODE='23514';\n END IF; END IF;",
)

SQL = r"""
DROP TRIGGER hold_immutable ON public.cases_stockhold;
CREATE FUNCTION public.reserve_hold_release() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP<>'DELETE' OR NOT EXISTS(SELECT 1 FROM public.cases_case c JOIN public.cases_stockreconciliation x ON x.case_id=c.id JOIN public.core_operation o ON o.id=x.operation_id JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.cases_caseevent e ON e.case_id=c.id AND e.operation_id=o.id JOIN public.cases_reconciledstock s ON s.reconciliation_id=x.id AND s.product_id=OLD.product_id WHERE c.stock_case_id=OLD.case_id AND c.status='RESOLVED' AND e.kind='STOCK_RECONCILED' AND o.actor_role='ADMIN' AND o.command_type='stock_reconciliations.confirm' AND i.state='READY') THEN RAISE EXCEPTION 'Hold can only be released by verified reconciliation' USING ERRCODE='23514'; END IF;
 RETURN OLD;
END; $$;
CREATE TRIGGER hold_immutable BEFORE UPDATE OR DELETE ON public.cases_stockhold FOR EACH ROW EXECUTE FUNCTION public.reserve_hold_release();

CREATE FUNCTION public.reserve_m6_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE op uuid;
BEGIN
 IF TG_TABLE_NAME='cases_case' THEN
  IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND ROW(NEW.id,NEW.stock_case_id,NEW.delivery_id,NEW.delivery_issue_id,NEW.origin_operation_id,NEW.kind) IS DISTINCT FROM ROW(OLD.id,OLD.stock_case_id,OLD.delivery_id,OLD.delivery_issue_id,OLD.origin_operation_id,OLD.kind)) THEN RAISE EXCEPTION 'Case declaration origin is immutable' USING ERRCODE='23514'; END IF;
  IF TG_OP='INSERT' AND NEW.stock_case_id IS NULL AND NEW.delivery_issue_id IS NULL AND NOT EXISTS(SELECT 1 FROM public.core_operation WHERE id=NEW.origin_operation_id AND actor_role='ADMIN' AND command_type='cases.open') THEN RAISE EXCEPTION 'Administrator case opening requires its explicit operation' USING ERRCODE='23514'; END IF;
  IF TG_OP='INSERT' AND (NEW.version<>0 OR NEW.status<>'OPEN' OR NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id WHERE o.id=NEW.origin_operation_id AND i.state='READY') OR (NEW.stock_case_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.cases_stockcase c WHERE c.id=NEW.stock_case_id AND c.operation_id=NEW.origin_operation_id)) OR (NEW.delivery_issue_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.requests_deliveryissue x WHERE x.id=NEW.delivery_issue_id AND x.delivery_id=NEW.delivery_id AND x.operation_id=NEW.origin_operation_id AND x.kind=NEW.kind))) THEN RAISE EXCEPTION 'Case requires matching original declaration' USING ERRCODE='23514'; END IF;
  RETURN NEW;
 ELSIF TG_TABLE_NAME='cases_caseevent' THEN
  op:=NEW.operation_id;
  IF NOT EXISTS(SELECT 1 FROM public.core_operation o WHERE o.id=op AND o.command_type=CASE NEW.kind WHEN 'REVIEWED' THEN 'cases.review' WHEN 'CORRECTION_BLOCKED' THEN 'cases.stock_conflict' WHEN 'DELIVERY_CORRECTED' THEN 'corrections.confirm' WHEN 'STOCK_RECONCILED' THEN 'stock_reconciliations.confirm' WHEN 'SHORTAGE_VERIFIED' THEN 'cases.resolve' ELSE '' END AND (NEW.kind IN ('DELIVERY_CORRECTED','STOCK_RECONCILED','CORRECTION_BLOCKED') OR o.resource_id=NEW.case_id)) THEN RAISE EXCEPTION 'Case event requires its exact decision command' USING ERRCODE='23514'; END IF;
  IF NEW.kind='CORRECTION_BLOCKED' AND NOT EXISTS(SELECT 1 FROM public.cases_stockcase sc JOIN public.core_operationintent fi ON fi.id=sc.failed_intent_id JOIN public.cases_case parent ON parent.id=NEW.case_id WHERE sc.operation_id=NEW.operation_id AND fi.command_type='corrections.confirm' AND fi.resource_id=parent.delivery_id) THEN RAISE EXCEPTION 'Blocked correction must link its failed intent and original case' USING ERRCODE='23514'; END IF;
  IF NEW.kind='SHORTAGE_VERIFIED' THEN
   IF NOT EXISTS(SELECT 1 FROM public.cases_case WHERE id=NEW.case_id AND kind='SHORTAGE') OR jsonb_typeof(NEW.details->'product_counts') IS DISTINCT FROM 'array' THEN RAISE EXCEPTION 'Only a verified shortage can resolve without quantity correction' USING ERRCODE='23514'; END IF;
   IF EXISTS(SELECT 1 FROM jsonb_array_elements(NEW.details->'product_counts') q LEFT JOIN public.inventory_stockbalance b ON b.product_id=(q->>'product_id')::uuid WHERE b.product_id IS NULL OR b.physical<>(q->>'counted_quantity')::bigint OR b.version<>(q->>'expected_stock_version')::bigint OR EXISTS(SELECT 1 FROM public.cases_stockhold h WHERE h.product_id=b.product_id) OR NOT EXISTS(SELECT 1 FROM public.cases_case c JOIN public.requests_deliveryline dl ON dl.delivery_id=c.delivery_id JOIN public.requests_requestline l ON l.id=dl.line_id WHERE c.id=NEW.case_id AND l.product_id=b.product_id))
   OR (SELECT count(DISTINCT q->>'product_id') FROM jsonb_array_elements(NEW.details->'product_counts') q)<>(SELECT count(*) FROM public.cases_case c JOIN public.requests_deliveryline dl ON dl.delivery_id=c.delivery_id WHERE c.id=NEW.case_id) THEN RAISE EXCEPTION 'Shortage verification must match all current stock counts' USING ERRCODE='23514'; END IF;
  END IF;
 ELSIF TG_TABLE_NAME='requests_requestdecision' THEN SELECT operation_id INTO op FROM public.requests_requestevent WHERE id=NEW.event_id;
  IF NOT NEW.verification THEN RAISE EXCEPTION 'Verification is mandatory' USING ERRCODE='23514'; END IF;
  IF NEW.next_operator_id IS DISTINCT FROM NEW.previous_operator_id AND NOT EXISTS(SELECT 1 FROM public.accounts_user WHERE id=NEW.next_operator_id AND is_active AND role='REPLENISHER') THEN RAISE EXCEPTION 'Active operator required' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='requests_decisionline' THEN SELECT e.operation_id INTO op FROM public.requests_requestdecision d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.id=NEW.decision_id;
 ELSIF TG_TABLE_NAME='requests_deliverycorrection' THEN SELECT e.operation_id INTO op FROM public.requests_requestdecision d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.id=NEW.decision_id;
 ELSIF TG_TABLE_NAME='requests_deliverycorrectionline' THEN SELECT e.operation_id INTO op FROM public.requests_deliverycorrection f JOIN public.requests_requestdecision d ON d.id=f.decision_id JOIN public.requests_requestevent e ON e.id=d.event_id WHERE f.id=NEW.correction_id;
 ELSIF TG_TABLE_NAME='cases_reconciledstock' THEN SELECT operation_id INTO op FROM public.cases_stockreconciliation WHERE id=NEW.reconciliation_id;
  IF EXISTS(SELECT 1 FROM public.inventory_stockentry s WHERE s.operation_id=op AND s.product_id=NEW.product_id) THEN
   IF ROW(NEW.physical_before,NEW.reserved_before,NEW.version_before) IS DISTINCT FROM (SELECT ROW(s.physical_before,s.reserved_before,s.version_before) FROM public.inventory_stockentry s WHERE s.operation_id=op AND s.product_id=NEW.product_id ORDER BY s.version_after LIMIT 1) THEN RAISE EXCEPTION 'Reconciliation must start from the actual ledger snapshot' USING ERRCODE='23514'; END IF;
  ELSE
   IF NOT EXISTS(SELECT 1 FROM public.inventory_stockbalance b WHERE b.product_id=NEW.product_id AND b.physical=NEW.physical_before AND b.reserved=NEW.reserved_before AND b.version=NEW.version_before AND b.physical=NEW.counted_quantity AND b.reserved=NEW.reserved_after) THEN RAISE EXCEPTION 'Unchanged reconciliation must match current stock' USING ERRCODE='23514'; END IF;
  END IF;
 ELSIF TG_TABLE_NAME='cases_stockreconciliation' THEN op:=NEW.operation_id;
  IF NOT EXISTS(SELECT 1 FROM public.core_operation WHERE id=op AND command_type='stock_reconciliations.confirm' AND resource_id=NEW.case_id) THEN RAISE EXCEPTION 'Reconciliation requires its typed command and case' USING ERRCODE='23514'; END IF;
 ELSE op:=NEW.operation_id;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id WHERE o.id=op AND o.actor_role='ADMIN' AND i.state='READY' AND o.command_type IN ('corrections.confirm','requests.close_remainder','requests.reopen','requests.reassign','stock_reconciliations.confirm','cases.review','cases.resolve','cases.stock_conflict')) THEN RAISE EXCEPTION 'M6 requires a new authorized administrator operation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER m6_case_guard BEFORE INSERT OR UPDATE OR DELETE ON public.cases_case FOR EACH ROW EXECUTE FUNCTION public.reserve_m6_guard();
CREATE FUNCTION public.reserve_m6_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE rid uuid; cid uuid; op uuid; n bigint; v bigint; c public.cases_case%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='cases_case' THEN cid:=NEW.id;
 ELSIF TG_TABLE_NAME='cases_caseevent' THEN cid:=NEW.case_id;op:=NEW.operation_id;
  IF NEW.kind='DELIVERY_CORRECTED' AND NOT EXISTS(SELECT 1 FROM public.requests_requestdecision d JOIN public.requests_requestevent e ON e.id=d.event_id JOIN public.requests_deliverycorrection f ON f.decision_id=d.id WHERE d.case_id=cid AND e.operation_id=op) THEN RAISE EXCEPTION 'Cannot resolve an uncorrected delivery case' USING ERRCODE='23514'; END IF;
  IF NEW.kind='STOCK_RECONCILED' AND (EXISTS(SELECT 1 FROM public.cases_stockhold h JOIN public.cases_case cs ON cs.stock_case_id=h.case_id WHERE cs.id=cid) OR NOT EXISTS(SELECT 1 FROM public.cases_stockreconciliation x WHERE x.operation_id=op AND x.case_id=cid)) THEN RAISE EXCEPTION 'Cannot resolve an unregularized stock conflict' USING ERRCODE='23514'; END IF;
  IF NOT ((NEW.kind IN ('REVIEWED','CORRECTION_BLOCKED') AND NEW.status='UNDER_REVIEW') OR (NEW.kind IN ('DELIVERY_CORRECTED','STOCK_RECONCILED','SHORTAGE_VERIFIED') AND NEW.status='RESOLVED')) OR (NEW.details->>'verification') IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'Invalid verified case transition' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='cases_stockreconciliation' THEN cid:=NEW.case_id;op:=NEW.operation_id;
  IF NOT NEW.verification OR NOT EXISTS(SELECT 1 FROM public.cases_reconciledstock WHERE reconciliation_id=NEW.id) OR (SELECT count(*) FROM public.cases_reconciledstock WHERE reconciliation_id=NEW.id)<>(SELECT count(*) FROM public.cases_stockcaseproduct cp JOIN public.cases_case cs ON cs.stock_case_id=cp.case_id WHERE cs.id=NEW.case_id) OR EXISTS(SELECT 1 FROM public.cases_reconciledstock rs WHERE rs.reconciliation_id=NEW.id AND NOT EXISTS(SELECT 1 FROM public.cases_stockcaseproduct cp JOIN public.cases_case cs ON cs.stock_case_id=cp.case_id WHERE cs.id=NEW.case_id AND cp.product_id=rs.product_id)) THEN RAISE EXCEPTION 'Reconciliation requires verified count facts' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='cases_reconciledstock' THEN
  SELECT case_id,operation_id INTO cid,op FROM public.cases_stockreconciliation WHERE id=NEW.reconciliation_id;
  PERFORM public.reserve_assert_stock(NEW.product_id);PERFORM public.reserve_assert_reservations(NEW.product_id);
  IF NEW.counted_quantity<>NEW.physical_before AND NOT EXISTS(SELECT 1 FROM public.inventory_stockentry s WHERE s.id=NEW.entry_id AND s.operation_id=op AND s.product_id=NEW.product_id AND s.kind='ADMIN_RECONCILIATION' AND s.physical_after=NEW.counted_quantity AND s.physical_before=NEW.physical_before AND s.delta_r=0) THEN RAISE EXCEPTION 'Reconciled count must match its unique ledger fact' USING ERRCODE='23514'; END IF;
  IF NEW.reserved_after<>NEW.reserved_before+COALESCE((SELECT sum(s.delta_r) FROM public.inventory_stockentry s WHERE s.operation_id=op AND s.product_id=NEW.product_id),0) OR NEW.version_after<>NEW.version_before+(SELECT count(*) FROM public.inventory_stockentry s WHERE s.operation_id=op AND s.product_id=NEW.product_id) THEN RAISE EXCEPTION 'Reconciliation must reconcile all releases and versions' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='requests_requestdecision' THEN SELECT request_id,operation_id INTO rid,op FROM public.requests_requestevent WHERE id=NEW.event_id;
 ELSIF TG_TABLE_NAME='requests_decisionline' THEN SELECT e.request_id,e.operation_id INTO rid,op FROM public.requests_requestdecision d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.id=NEW.decision_id;
 ELSIF TG_TABLE_NAME='requests_deliverycorrection' THEN SELECT e.request_id,e.operation_id INTO rid,op FROM public.requests_requestdecision d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.id=NEW.decision_id;
 ELSE SELECT e.request_id,e.operation_id INTO rid,op FROM public.requests_deliverycorrection f JOIN public.requests_requestdecision d ON d.id=f.decision_id JOIN public.requests_requestevent e ON e.id=d.event_id WHERE f.id=NEW.correction_id;
 END IF;
 IF rid IS NOT NULL THEN PERFORM public.reserve_assert_request(rid); END IF;
 IF op IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id WHERE o.id=op AND i.state='CONFIRMED' AND EXISTS(SELECT 1 FROM public.audit_auditevent a WHERE a.operation_id=op)) THEN RAISE EXCEPTION 'M6 facts require confirmed audit' USING ERRCODE='23514'; END IF;
 IF cid IS NOT NULL THEN
  SELECT * INTO c FROM public.cases_case WHERE id=cid;
  SELECT count(*),COALESCE(max(revision),0) INTO n,v FROM public.cases_caseevent WHERE case_id=cid;
  IF n<>v OR c.version<>v OR c.status<>COALESCE((SELECT status FROM public.cases_caseevent WHERE case_id=cid ORDER BY revision DESC LIMIT 1),'OPEN') THEN RAISE EXCEPTION 'Case projection must match immutable events' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER m6_case_consistency AFTER INSERT OR UPDATE ON public.cases_case DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m6_consistency();
"""
SQL += r"""
CREATE FUNCTION public.reserve_m6_stock_origin() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.kind='ADMIN_RECONCILIATION' AND NOT EXISTS(SELECT 1 FROM public.cases_reconciledstock l JOIN public.cases_stockreconciliation r ON r.id=l.reconciliation_id WHERE l.entry_id=NEW.id AND l.product_id=NEW.product_id AND r.operation_id=NEW.operation_id AND l.counted_quantity=NEW.physical_after) THEN RAISE EXCEPTION 'Administrative count entry requires its unique reconciliation line' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER m6_stock_origin AFTER INSERT ON public.inventory_stockentry DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m6_stock_origin();
"""

for table in [
    "requests_requestdecision",
    "requests_decisionline",
    "requests_deliverycorrection",
    "requests_deliverycorrectionline",
    "cases_caseevent",
    "cases_stockreconciliation",
    "cases_reconciledstock",
]:
    SQL += f"CREATE TRIGGER m6_guard BEFORE INSERT ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.reserve_m6_guard();\nCREATE TRIGGER m6_immutable BEFORE UPDATE OR DELETE ON public.{table} FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();\nCREATE CONSTRAINT TRIGGER m6_consistency AFTER INSERT ON public.{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m6_consistency();\n"


class Migration(migrations.Migration):
    dependencies = [("cases", "0004_existing_case_projections"), ("requests", "0010_m6_integrity")]
    operations = [migrations.RunSQL(consistency + SQL)]
