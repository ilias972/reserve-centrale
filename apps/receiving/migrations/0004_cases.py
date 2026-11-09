"""Étendre le workflow de dossiers M6 en conservant ses gardes pour tous les faits anciens."""

from importlib import import_module
from django.db import migrations

SQL = r"""
CREATE FUNCTION public.reserve_m8_case_input(value jsonb, oldvalue jsonb, tab text, action text) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
#variable_conflict use_variable
DECLARE c public.cases_case%ROWTYPE; o public.core_operation%ROWTYPE; cid uuid; receipt_id uuid; kind text; cmd text; resource uuid; required_role text;
BEGIN
 IF action='DELETE' THEN RAISE EXCEPTION 'Case history cannot be deleted' USING ERRCODE='23514'; END IF;
 IF tab='cases_case' THEN
  IF action='UPDATE' THEN
   IF value-ARRAY['version','status'] IS DISTINCT FROM oldvalue-ARRAY['version','status'] THEN RAISE EXCEPTION 'Receiving case origin is immutable' USING ERRCODE='23514'; END IF;
   RETURN;
  END IF;
  SELECT * INTO o FROM public.core_operation WHERE id=(value->>'origin_operation_id')::uuid;
  IF (value->>'version')::bigint<>0 OR value->>'status'<>'OPEN' OR NOT EXISTS(SELECT 1 FROM public.core_operationintent i WHERE i.id=o.intent_id AND i.state='READY') THEN RAISE EXCEPTION 'New case must cite its new declaration' USING ERRCODE='23514'; END IF;
  kind:=value->>'kind';receipt_id:=(value->>'receipt_id')::uuid;
  IF kind='RECEIPT_ERROR' THEN
   IF receipt_id IS NULL OR NOT ((o.command_type='receipts.report_error' AND o.resource_id=receipt_id AND o.actor_role='REPLENISHER' AND EXISTS(SELECT 1 FROM public.receiving_receipt r JOIN public.receiving_orderevent e ON e.id=r.event_id JOIN public.core_operation source ON source.id=e.operation_id WHERE r.id=receipt_id AND source.actor_id=o.actor_id)) OR (o.command_type='cases.open' AND o.actor_role='ADMIN' AND o.resource_id IS NULL)) THEN RAISE EXCEPTION 'Receipt error requires its actual operator or verified administrator' USING ERRCODE='23514'; END IF;
  ELSIF receipt_id IS NOT NULL THEN
   IF kind NOT IN ('SURPLUS','NONCONFORMITY') OR NOT ((o.command_type='receipts.create' AND o.actor_role='REPLENISHER') OR (o.command_type='corrections.confirm' AND o.actor_role='ADMIN')) THEN RAISE EXCEPTION 'Receipt exception needs its actual receipt or correction' USING ERRCODE='23514'; END IF;
  ELSIF o.actor_role<>'REPLENISHER' OR o.command_type<>(CASE kind WHEN 'UNKNOWN_ORDER' THEN 'receiving.unknown' WHEN 'SURPLUS' THEN 'receiving.extra' ELSE 'receiving.late' END) THEN RAISE EXCEPTION 'Arrival exception needs its real declaration' USING ERRCODE='23514'; END IF;
  RETURN;
 END IF;
 cid:=(value->>'case_id')::uuid;kind:=value->>'kind';
 SELECT * INTO c FROM public.cases_case WHERE id=cid;
 SELECT * INTO o FROM public.core_operation WHERE id=(value->>'operation_id')::uuid;
 IF (value->>'revision')::bigint<>c.version+1 OR value->'details'->>'verification' IS DISTINCT FROM 'true' THEN RAISE EXCEPTION 'Case decision requires current version and verification' USING ERRCODE='23514'; END IF;
 cmd:=CASE kind WHEN 'REVIEWED' THEN 'cases.review' WHEN 'QUARANTINE_ACCEPTED' THEN 'quarantine.accept_surplus' WHEN 'QUARANTINE_REFUSED' THEN 'quarantine.refuse' WHEN 'EXTERNAL_VERIFIED' THEN 'receiving.case_resolve' WHEN 'UNKNOWN_LINKED' THEN 'cases.link_order' WHEN 'REMAINDER_REINSTATED' THEN 'orders.reinstate_remainder' WHEN 'ISOLATION_CONSUMED' THEN 'receipts.create' WHEN 'CORRECTION_BLOCKED' THEN 'cases.stock_conflict' WHEN 'ISOLATION_CORRECTED' THEN 'corrections.confirm' WHEN 'RECEIPT_CORRECTED' THEN 'corrections.confirm' ELSE '' END;
 required_role:=CASE WHEN kind='ISOLATION_CONSUMED' THEN 'REPLENISHER' ELSE 'ADMIN' END;
 IF cmd='' OR o.command_type<>cmd OR o.actor_role<>required_role OR NOT EXISTS(SELECT 1 FROM public.core_operationintent WHERE id=o.intent_id AND state='READY') THEN RAISE EXCEPTION 'Case event requires its exact receiving action' USING ERRCODE='23514'; END IF;
 IF kind IN ('REVIEWED','QUARANTINE_REFUSED','UNKNOWN_LINKED','REMAINDER_REINSTATED','CORRECTION_BLOCKED') AND value->>'status'<>'UNDER_REVIEW' THEN RAISE EXCEPTION 'This action does not resolve an isolation' USING ERRCODE='23514'; END IF;
 IF value->>'status' NOT IN ('UNDER_REVIEW','RESOLVED') THEN RAISE EXCEPTION 'Invalid receiving case status' USING ERRCODE='23514'; END IF;
 IF kind IN ('REVIEWED','UNKNOWN_LINKED','EXTERNAL_VERIFIED') AND o.resource_id IS DISTINCT FROM cid THEN RAISE EXCEPTION 'Case action resource mismatch' USING ERRCODE='23514'; END IF;
 IF kind IN ('QUARANTINE_ACCEPTED','QUARANTINE_REFUSED') AND NOT EXISTS(SELECT 1 FROM public.receiving_quarantinedecision d JOIN public.receiving_quarantineitem i ON i.id=d.item_id WHERE d.operation_id=o.id AND i.case_id=cid AND o.resource_id=i.id AND d.kind=CASE WHEN kind='QUARANTINE_ACCEPTED' THEN 'ACCEPT' ELSE 'REFUSE' END) THEN RAISE EXCEPTION 'Missing surplus decision' USING ERRCODE='23514'; END IF;
 IF kind='UNKNOWN_LINKED' AND NOT EXISTS(SELECT 1 FROM public.receiving_unknownlink u JOIN public.receiving_arrivalreport h ON h.id=u.report_id WHERE u.operation_id=o.id AND h.case_id=cid) THEN RAISE EXCEPTION 'Missing verified parcel link' USING ERRCODE='23514'; END IF;
 IF kind='REMAINDER_REINSTATED' AND NOT EXISTS(SELECT 1 FROM public.receiving_reinstatement h JOIN public.receiving_orderevent e ON e.id=h.event_id JOIN public.receiving_arrivalreport r ON r.id=h.report_id WHERE e.operation_id=o.id AND r.case_id=cid AND o.resource_id=e.order_id) THEN RAISE EXCEPTION 'Missing explicit reinstatement' USING ERRCODE='23514'; END IF;
 IF kind='ISOLATION_CONSUMED' AND NOT EXISTS(SELECT 1 FROM public.receiving_sourceconsumption s JOIN public.receiving_quarantineitem i ON i.id=s.item_id JOIN public.receiving_receiptline l ON l.id=s.line_id JOIN public.receiving_receipt r ON r.id=l.receipt_id JOIN public.receiving_orderevent e ON e.id=r.event_id WHERE e.operation_id=o.id AND i.case_id=cid AND o.resource_id=e.order_id) THEN RAISE EXCEPTION 'Missing receipt consumption' USING ERRCODE='23514'; END IF;
 IF kind IN ('ISOLATION_CORRECTED','RECEIPT_CORRECTED') AND NOT EXISTS(SELECT 1 FROM public.receiving_receiptcorrection h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE e.operation_id=o.id AND h.receipt_id=o.resource_id AND ((kind='RECEIPT_CORRECTED' AND h.case_id=cid) OR (kind='ISOLATION_CORRECTED' AND (c.receipt_id=h.receipt_id OR EXISTS(SELECT 1 FROM public.receiving_sourceconsumption s JOIN public.receiving_receiptline l ON l.id=s.line_id JOIN public.receiving_quarantineitem i ON i.id=s.item_id WHERE l.receipt_id=h.receipt_id AND i.case_id=cid))))) THEN RAISE EXCEPTION 'Missing linked receipt correction' USING ERRCODE='23514'; END IF;
 IF kind='CORRECTION_BLOCKED' AND NOT EXISTS(SELECT 1 FROM public.cases_stockcase s JOIN public.core_operationintent fi ON fi.id=s.failed_intent_id WHERE s.operation_id=o.id AND fi.command_type='corrections.confirm' AND fi.resource_id=c.receipt_id) THEN RAISE EXCEPTION 'Missing blocked receipt correction incident' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_m8_assert_case(cid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE c public.cases_case%ROWTYPE; n bigint; v bigint; last_kind text; last_op uuid; report_row public.receiving_arrivalreport%ROWTYPE;
BEGIN
 SELECT * INTO c FROM public.cases_case WHERE id=cid;
 IF c.kind NOT IN ('SURPLUS','NONCONFORMITY','UNKNOWN_ORDER','LATE_CANCELLED_DELIVERY','RECEIPT_ERROR') THEN RETURN; END IF;
 SELECT count(*),coalesce(max(revision),0) INTO n,v FROM public.cases_caseevent WHERE case_id=cid;
 SELECT kind,operation_id INTO last_kind,last_op FROM public.cases_caseevent WHERE case_id=cid ORDER BY revision DESC LIMIT 1;
 IF n<>v OR c.version<>v OR c.status<>coalesce((SELECT status FROM public.cases_caseevent WHERE case_id=cid ORDER BY revision DESC LIMIT 1),'OPEN') THEN RAISE EXCEPTION 'Receiving case projection differs from its immutable events' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.audit_auditevent a ON a.operation_id=o.id WHERE o.id=c.origin_operation_id AND i.state='CONFIRMED') OR EXISTS(SELECT 1 FROM public.cases_caseevent e JOIN public.core_operation o ON o.id=e.operation_id JOIN public.core_operationintent i ON i.id=o.intent_id WHERE e.case_id=cid AND (i.state<>'CONFIRMED' OR NOT EXISTS(SELECT 1 FROM public.audit_auditevent WHERE operation_id=o.id))) THEN RAISE EXCEPTION 'Case declaration and decisions require confirmed audit' USING ERRCODE='23514'; END IF;
 IF c.kind='RECEIPT_ERROR' THEN
  IF c.receipt_id IS NULL THEN RAISE EXCEPTION 'Missing original receipt' USING ERRCODE='23514'; END IF;
  IF c.status='RESOLVED' AND (last_kind<>'RECEIPT_CORRECTED' OR NOT EXISTS(SELECT 1 FROM public.receiving_receiptcorrection h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE h.case_id=cid AND e.operation_id=last_op)) THEN RAISE EXCEPTION 'Receipt error cannot resolve without effective verified correction' USING ERRCODE='23514'; END IF;
 ELSIF c.receipt_id IS NOT NULL THEN
  IF c.kind NOT IN ('SURPLUS','NONCONFORMITY') OR NOT EXISTS(SELECT 1 FROM public.receiving_quarantineitem i JOIN public.receiving_receiptline l ON l.id=i.receipt_line_id WHERE i.case_id=cid AND l.receipt_id=c.receipt_id AND i.kind=CASE WHEN c.kind='SURPLUS' THEN 'SURPLUS' ELSE 'NONCONFORMING' END) OR NOT EXISTS(SELECT 1 FROM public.receiving_receipt r JOIN public.receiving_orderevent e ON e.id=r.event_id WHERE r.id=c.receipt_id AND e.operation_id=c.origin_operation_id UNION ALL SELECT 1 FROM public.receiving_receiptcorrection h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE h.receipt_id=c.receipt_id AND e.operation_id=c.origin_operation_id) THEN RAISE EXCEPTION 'Receipt exception requires actual typed isolation' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT * INTO report_row FROM public.receiving_arrivalreport WHERE case_id=cid;
  IF report_row.id IS NULL OR report_row.operation_id<>c.origin_operation_id OR report_row.kind<>c.kind THEN RAISE EXCEPTION 'Missing original arrival report' USING ERRCODE='23514'; END IF;
 END IF;
 IF c.kind<>'RECEIPT_ERROR' AND c.status='RESOLVED' THEN
  IF EXISTS(SELECT 1 FROM public.receiving_quarantineitem i WHERE i.case_id=cid AND (public.reserve_m8_item_state(i.id)->>'remaining')::bigint<>0) THEN RAISE EXCEPTION 'Isolated goods still require effective treatment' USING ERRCODE='23514'; END IF;
  IF NOT EXISTS(SELECT 1 FROM public.receiving_quarantineitem WHERE case_id=cid) AND NOT EXISTS(SELECT 1 FROM public.receiving_quarantinedecision d JOIN public.receiving_arrivalreport r ON r.id=d.report_id WHERE r.case_id=cid AND d.kind='EXTERNAL') THEN RAISE EXCEPTION 'Unidentified parcel requires verified external treatment' USING ERRCODE='23514'; END IF;
 END IF;
END; $$;
"""

legacy = import_module("apps.cases.migrations.0005_m6_guards").SQL
for function, following in [
    ("reserve_m6_guard", "CREATE TRIGGER m6_case_guard"),
    ("reserve_m6_consistency", "CREATE CONSTRAINT TRIGGER m6_case_consistency"),
]:
    text = legacy.split("CREATE FUNCTION public." + function + "()", 1)[1].split(following, 1)[0]
    text = "CREATE OR REPLACE FUNCTION public." + function + "()" + text
    condition = """(TG_TABLE_NAME='cases_case' AND coalesce(to_jsonb(NEW)->>'kind',to_jsonb(OLD)->>'kind') IN ('SURPLUS','NONCONFORMITY','UNKNOWN_ORDER','LATE_CANCELLED_DELIVERY','RECEIPT_ERROR')) OR (TG_TABLE_NAME='cases_caseevent' AND EXISTS(SELECT 1 FROM public.cases_case m8case WHERE m8case.id=(to_jsonb(NEW)->>'case_id')::uuid AND m8case.kind IN ('SURPLUS','NONCONFORMITY','UNKNOWN_ORDER','LATE_CANCELLED_DELIVERY','RECEIPT_ERROR')))"""
    if function.endswith("guard"):
        extra = f"IF {condition} THEN PERFORM public.reserve_m8_case_input(to_jsonb(NEW),to_jsonb(OLD),TG_TABLE_NAME,TG_OP); RETURN NEW; END IF;\n"
    else:
        extra = f"IF {condition} THEN PERFORM public.reserve_m8_assert_case(CASE WHEN TG_TABLE_NAME='cases_case' THEN (to_jsonb(NEW)->>'id')::uuid ELSE (to_jsonb(NEW)->>'case_id')::uuid END); RETURN NULL; END IF;\n"
    text = text.replace("BEGIN\n", "BEGIN\n " + extra, 1)
    SQL += text

# Les mutations d'isolation doivent également revalider la projection de dossier.
SQL += r"""
CREATE FUNCTION public.reserve_m8_case_from_item() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE cid uuid; iid uuid;
BEGIN
 IF TG_TABLE_NAME='receiving_quarantineitem' THEN iid:=NEW.id;
 ELSIF TG_TABLE_NAME='receiving_quarantinedecision' THEN iid:=NEW.item_id;
 ELSIF TG_TABLE_NAME='receiving_sourceconsumption' THEN iid:=NEW.item_id;
 ELSIF TG_TABLE_NAME='receiving_receiptcorrectionline' THEN
  FOR cid IN SELECT case_id FROM public.receiving_quarantineitem WHERE receipt_line_id=NEW.line_id UNION SELECT i.case_id FROM public.receiving_sourceconsumption s JOIN public.receiving_quarantineitem i ON i.id=s.item_id WHERE s.line_id=NEW.line_id LOOP PERFORM public.reserve_m8_assert_case(cid); END LOOP; RETURN NULL;
 END IF;
 IF iid IS NOT NULL THEN SELECT case_id INTO cid FROM public.receiving_quarantineitem WHERE id=iid; PERFORM public.reserve_m8_assert_case(cid); END IF;
 RETURN NULL;
END; $$;
"""
for table in ["quarantineitem", "quarantinedecision", "sourceconsumption", "receiptcorrectionline"]:
    SQL += f"CREATE CONSTRAINT TRIGGER m8_case_from_item AFTER INSERT ON public.receiving_{table} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m8_case_from_item();\n"


class Migration(migrations.Migration):
    dependencies = [("receiving", "0003_integrity")]
    operations = [migrations.RunSQL(SQL)]
