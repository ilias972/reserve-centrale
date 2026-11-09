"""Étendre les gardes sans retirer celles des demandes M0–M5 non corrigées."""

from importlib import import_module
from django.db import migrations

base = import_module("apps.requests.migrations.0008_refusal_integrity")
legacy = base.NEW_ASSERT.replace(
    "FUNCTION public.reserve_assert_request(", "FUNCTION public.reserve_assert_request_m5("
)
guard = base.NEW_GUARD.replace(
    "o.command_type IN ('requests.claim','requests.deliver','requests.refuse')))",
    "o.command_type IN ('requests.claim','requests.deliver','requests.refuse')) OR (o.actor_role='ADMIN' AND o.command_type IN ('corrections.confirm','requests.close_remainder','requests.reopen','requests.reassign','stock_reconciliations.confirm')))",
)
if guard == base.NEW_GUARD:
    raise RuntimeError("La garde historique ne correspond pas à l’extension M6.")
stockbase = import_module("apps.inventory.migrations.0002_integrity").FORWARD
stockguard = stockbase.split("CREATE FUNCTION public.reserve_stock_insert_guard()", 1)[1].split(
    "CREATE TRIGGER stock_insert_guard", 1
)[0]
stockguard = "CREATE OR REPLACE FUNCTION public.reserve_stock_insert_guard()" + stockguard
stockguard = stockguard.replace(
    "AND (NEW.delta_r>0 OR NEW.delta_p<0) THEN",
    "AND (NEW.delta_r>0 OR NEW.delta_p<0) AND NOT (NEW.kind='ADMIN_RECONCILIATION' AND o.actor_role='ADMIN' AND o.command_type='stock_reconciliations.confirm') THEN",
)
stockguard = stockguard.replace(
    " RETURN NEW;\nEND;",
    " IF NEW.kind IN ('DELIVERY_CORRECTION','ADMIN_RECONCILIATION') AND (o.actor_role<>'ADMIN' OR o.command_type NOT IN ('corrections.confirm','requests.reopen','stock_reconciliations.confirm')) THEN RAISE EXCEPTION 'Administrative stock correction requires authorized origin' USING ERRCODE='23514'; END IF;\n RETURN NEW;\nEND;",
)
SQL = r"""
CREATE FUNCTION public.reserve_operator_at(rid uuid, rev bigint) RETURNS uuid LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT CASE WHEN e.kind='CLAIMED' THEN o.actor_id ELSE d.next_operator_id END FROM public.requests_requestevent e
 JOIN public.core_operation o ON o.id=e.operation_id LEFT JOIN public.requests_requestdecision d ON d.event_id=e.id
 WHERE e.request_id=rid AND e.kind IN ('CLAIMED','REASSIGNED') AND e.revision<rev ORDER BY e.revision DESC LIMIT 1;
$$;
CREATE OR REPLACE FUNCTION public.reserve_assert_request(rid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE r public.requests_internalrequest%ROWTYPE; n bigint; vmax bigint; expected text; anchor uuid;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM public.requests_requestdecision d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE e.request_id=rid) THEN PERFORM public.reserve_assert_request_m5(rid); RETURN; END IF;
 SELECT * INTO r FROM public.requests_internalrequest WHERE id=rid;
 SELECT count(*),max(revision) INTO n,vmax FROM public.requests_requestevent WHERE request_id=rid;
 IF r.version<>vmax OR n<>vmax+1 OR NOT EXISTS(SELECT 1 FROM public.requests_requestevent WHERE request_id=rid AND revision=0 AND kind='CREATED' AND operation_id=r.creation_operation_id)
 OR (SELECT count(*) FROM public.requests_requestevent WHERE request_id=rid AND kind='CLAIMED')>1
 OR r.assigned_to_id IS DISTINCT FROM public.reserve_operator_at(rid,r.version+1) THEN RAISE EXCEPTION 'Invalid administrative request event or assignment chain' USING ERRCODE='23514'; END IF;
 SELECT d.operation_id INTO anchor FROM public.requests_delivery d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.request_id=rid ORDER BY e.revision LIMIT 1;
 IF r.first_delivery_operation_id IS DISTINCT FROM anchor THEN RAISE EXCEPTION 'Historical delivery anchor cannot change' USING ERRCODE='23514'; END IF;
 expected:=CASE WHEN EXISTS(SELECT 1 FROM public.requests_reservation z JOIN public.requests_requestline l ON l.id=z.line_id WHERE l.request_id=rid AND z.remaining>0)
 THEN CASE WHEN EXISTS(SELECT 1 FROM public.requests_requestline WHERE request_id=rid AND delivered>0) THEN 'PARTIAL_OPEN' WHEN r.assigned_to_id IS NOT NULL THEN 'PREPARING' ELSE 'NEW' END
 WHEN EXISTS(SELECT 1 FROM public.requests_requestevent WHERE request_id=rid AND kind='CANCELLED') THEN 'CANCELLED' WHEN EXISTS(SELECT 1 FROM public.requests_requestevent WHERE request_id=rid AND kind='REFUSED') THEN 'REFUSED' WHEN EXISTS(SELECT 1 FROM public.requests_requestline WHERE request_id=rid AND closed>0) THEN 'PARTIAL_CLOSED' ELSE 'DELIVERED' END;
 IF r.status<>expected OR (r.status='PARTIAL_CLOSED' AND r.closure_reason NOT IN ('SHORTAGE','REMAINDER_ABANDONED','ADMIN_RECONCILIATION','DELIVERY_CORRECTION')) THEN RAISE EXCEPTION 'Administrative state must match quantities and cause' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestline l JOIN public.requests_reservation z ON z.line_id=l.id WHERE l.request_id=rid AND (
 l.quantity<>l.delivered+l.closed+z.remaining OR z.product_id<>l.product_id
 OR l.delivered<>COALESCE((SELECT sum(handed) FROM public.requests_deliveryline WHERE line_id=l.id),0)+COALESCE((SELECT sum(c.effective_after-c.effective_before) FROM public.requests_deliverycorrectionline c JOIN public.requests_deliveryline dl ON dl.id=c.delivery_line_id WHERE dl.line_id=l.id),0)
 OR l.closed<>-COALESCE((SELECT sum(se.delta_r) FROM public.requests_reservationchange rc JOIN public.requests_requestevent re ON re.id=rc.event_id JOIN public.inventory_stockentry se ON se.id=rc.entry_id WHERE rc.line_id=l.id AND re.kind IN ('CANCELLED','REFUSED')),0)+COALESCE((SELECT sum(missing) FROM public.requests_deliveryline WHERE line_id=l.id),0)+COALESCE((SELECT sum(q.closed_after-q.closed_before) FROM public.requests_decisionline q WHERE q.line_id=l.id),0)
 OR z.remaining<>COALESCE((SELECT sum(s.delta_r) FROM public.requests_reservationchange c JOIN public.inventory_stockentry s ON s.id=c.entry_id WHERE c.line_id=l.id),0)
 OR z.version<>(SELECT count(DISTINCT event_id)-1 FROM public.requests_reservationchange WHERE line_id=l.id)
 )) THEN RAISE EXCEPTION 'Administrative facts must reconstruct Q L C U and reservations' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestevent e JOIN public.core_operation o ON o.id=e.operation_id JOIN public.core_operationintent i ON i.id=o.intent_id WHERE e.request_id=rid AND (
 i.state<>'CONFIRMED'
 OR (e.kind='CREATED' AND (o.actor_role<>'ASSISTANT' OR o.actor_id<>r.assistant_id OR o.command_type<>'requests.create'))
 OR (e.kind IN ('CANCELLED','REFUSED') AND (r.first_delivery_operation_id IS NOT NULL OR e.revision<>r.version)) OR (e.kind='CANCELLED' AND (o.actor_role<>'ASSISTANT' OR o.actor_id<>r.assistant_id OR o.resource_id IS DISTINCT FROM rid)) OR (e.kind='REFUSED' AND (o.actor_role<>'REPLENISHER' OR o.actor_id IS DISTINCT FROM public.reserve_operator_at(rid,e.revision) OR o.resource_id IS DISTINCT FROM rid))
 OR (e.kind IN ('CLAIMED','DELIVERED') AND (o.actor_role<>'REPLENISHER' OR o.actor_id IS DISTINCT FROM CASE WHEN e.kind='CLAIMED' THEN public.reserve_operator_at(rid,e.revision+1) ELSE public.reserve_operator_at(rid,e.revision) END OR o.resource_id IS DISTINCT FROM rid))
 OR (e.kind IN ('CORRECTED','CLOSED','REOPENED','REASSIGNED','RECONCILED') AND (o.actor_role<>'ADMIN' OR NOT EXISTS(SELECT 1 FROM public.requests_requestdecision d WHERE d.event_id=e.id AND d.verification)))
 OR o.command_type<>CASE e.kind WHEN 'CREATED' THEN 'requests.create' WHEN 'CANCELLED' THEN 'requests.cancel' WHEN 'REFUSED' THEN 'requests.refuse' WHEN 'CLAIMED' THEN 'requests.claim' WHEN 'DELIVERED' THEN 'requests.deliver' WHEN 'CORRECTED' THEN 'corrections.confirm' WHEN 'CLOSED' THEN 'requests.close_remainder' WHEN 'REOPENED' THEN 'requests.reopen' WHEN 'REASSIGNED' THEN 'requests.reassign' WHEN 'RECONCILED' THEN 'stock_reconciliations.confirm' ELSE '' END
 OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=o.id AND b.aggregate_id=rid AND b.event_type=CASE e.kind WHEN 'CREATED' THEN 'request.created' WHEN 'CANCELLED' THEN 'request.cancelled' WHEN 'REFUSED' THEN 'request.refused' WHEN 'CLAIMED' THEN 'request.claimed' WHEN 'DELIVERED' THEN 'request.delivered' WHEN 'CORRECTED' THEN 'request.corrected' WHEN 'CLOSED' THEN 'request.remainder_closed' WHEN 'REOPENED' THEN 'request.reopened' WHEN 'REASSIGNED' THEN 'request.reassigned' WHEN 'RECONCILED' THEN 'request.reconciled' ELSE '' END)
 )) THEN RAISE EXCEPTION 'Administrative event requires matching role operation and outbox' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_delivery d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.request_id=rid AND (d.operation_id<>e.operation_id OR e.kind<>'DELIVERED' OR d.operator_id IS DISTINCT FROM public.reserve_operator_at(rid,e.revision)
 OR NOT EXISTS(SELECT 1 FROM public.core_operation o WHERE o.id=d.operation_id AND o.actor_id=d.operator_id)
 OR (SELECT count(*) FROM public.requests_deliveryline WHERE delivery_id=d.id)<>(SELECT count(*) FROM public.requests_requestline WHERE request_id=rid))) THEN RAISE EXCEPTION 'Original delivery and its author must remain valid' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_deliveryline dl JOIN public.requests_delivery d ON d.id=dl.delivery_id JOIN public.requests_requestevent e ON e.id=d.event_id JOIN public.requests_requestline l ON l.id=dl.line_id WHERE d.request_id=rid AND (
 l.request_id<>rid OR dl.remaining_before<>l.quantity-COALESCE((SELECT sum(p.handed+p.missing) FROM public.requests_deliveryline p JOIN public.requests_delivery pd ON pd.id=p.delivery_id JOIN public.requests_requestevent pe ON pe.id=pd.event_id WHERE p.line_id=l.id AND pe.revision<e.revision),0)-COALESCE((SELECT sum(q.delivered_after-q.delivered_before+q.closed_after-q.closed_before) FROM public.requests_decisionline q JOIN public.requests_requestdecision x ON x.id=q.decision_id JOIN public.requests_requestevent xe ON xe.id=x.event_id WHERE q.line_id=l.id AND xe.revision<e.revision),0)
 OR (d.mode='COMPLETE' AND (dl.handed<>dl.remaining_before OR dl.missing<>0)) OR (d.mode='LATER' AND dl.missing<>0) OR (d.mode='SHORTAGE' AND dl.handed+dl.missing<>dl.remaining_before) OR (dl.handed>0 AND (SELECT count(*) FROM public.requests_reservationchange c JOIN public.inventory_stockentry se ON se.id=c.entry_id WHERE c.event_id=e.id AND c.line_id=l.id AND se.kind='DELIVER')<>1) OR (dl.missing>0 AND ((SELECT count(*) FROM public.requests_reservationchange c JOIN public.inventory_stockentry se ON se.id=c.entry_id WHERE c.event_id=e.id AND c.line_id=l.id AND se.kind='RELEASE')<>1 OR (SELECT count(*) FROM public.requests_reservationchange c JOIN public.inventory_stockentry se ON se.id=c.entry_id WHERE c.event_id=e.id AND c.line_id=l.id AND se.kind='SHORTAGE')<>1))
 )) THEN RAISE EXCEPTION 'Delivery must use actual historical remainder' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_decisionline q JOIN public.requests_requestdecision d ON d.id=q.decision_id JOIN public.requests_requestevent e ON e.id=d.event_id JOIN public.requests_requestline l ON l.id=q.line_id LEFT JOIN public.inventory_stockentry s ON s.id=q.entry_id WHERE e.request_id=rid AND (
 l.request_id<>rid OR q.delivered_before<>COALESCE((SELECT sum(dl.handed) FROM public.requests_deliveryline dl JOIN public.requests_delivery od ON od.id=dl.delivery_id JOIN public.requests_requestevent oe ON oe.id=od.event_id WHERE dl.line_id=l.id AND oe.revision<e.revision),0)+COALESCE((SELECT sum(prev.delivered_after-prev.delivered_before) FROM public.requests_decisionline prev JOIN public.requests_requestdecision pd ON pd.id=prev.decision_id JOIN public.requests_requestevent pe ON pe.id=pd.event_id WHERE prev.line_id=l.id AND pe.revision<e.revision),0) OR q.closed_before<>COALESCE((SELECT sum(dl.missing) FROM public.requests_deliveryline dl JOIN public.requests_delivery od ON od.id=dl.delivery_id JOIN public.requests_requestevent oe ON oe.id=od.event_id WHERE dl.line_id=l.id AND oe.revision<e.revision),0)+COALESCE((SELECT sum(prev.closed_after-prev.closed_before) FROM public.requests_decisionline prev JOIN public.requests_requestdecision pd ON pd.id=prev.decision_id JOIN public.requests_requestevent pe ON pe.id=pd.event_id WHERE prev.line_id=l.id AND pe.revision<e.revision),0) OR q.delivered_before+q.closed_before+q.reserved_before<>l.quantity OR q.delivered_after+q.closed_after+q.reserved_after<>l.quantity
 OR (q.entry_id IS NULL AND ROW(q.delivered_before,q.closed_before,q.reserved_before) IS DISTINCT FROM ROW(q.delivered_after,q.closed_after,q.reserved_after))
 OR (q.entry_id IS NOT NULL AND (s.operation_id<>e.operation_id OR s.product_id<>l.product_id OR s.delta_p<>q.delivered_before-q.delivered_after OR s.delta_r<>q.reserved_after-q.reserved_before OR NOT EXISTS(SELECT 1 FROM public.requests_reservationchange c WHERE c.entry_id=s.id AND c.event_id=e.id AND c.line_id=l.id)))
 OR (e.kind IN ('CLOSED','RECONCILED') AND (q.delivered_after<>q.delivered_before OR q.closed_after<q.closed_before OR (s.id IS NOT NULL AND s.kind<>'RELEASE')))
 OR (e.kind IN ('CORRECTED','REOPENED') AND s.id IS NOT NULL AND s.kind<>'DELIVERY_CORRECTION')
 OR (e.kind='REOPENED' AND (q.delivered_before<>q.delivered_after OR q.reserved_before<>0 OR q.reserved_after<0)) OR (e.kind='CORRECTED' AND ((d.disposition='KEEP_OPEN' AND q.closed_after<>q.closed_before) OR (d.disposition='KEEP_CLOSED' AND (q.reserved_before<>0 OR q.reserved_after<>0)) OR (d.disposition='REOPEN' AND q.reserved_before<>0) OR d.disposition NOT IN ('KEEP_OPEN','KEEP_CLOSED','REOPEN')))
 OR (e.kind='CORRECTED' AND q.delivered_after-q.delivered_before<>COALESCE((SELECT sum(c.effective_after-c.effective_before) FROM public.requests_deliverycorrectionline c JOIN public.requests_deliverycorrection f ON f.id=c.correction_id JOIN public.requests_deliveryline dl ON dl.id=c.delivery_line_id WHERE f.decision_id=d.id AND dl.line_id=l.id),0))
 )) THEN RAISE EXCEPTION 'Invalid typed administrative line or ledger delta' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_deliverycorrectionline c JOIN public.requests_deliverycorrection f ON f.id=c.correction_id JOIN public.requests_requestdecision d ON d.id=f.decision_id JOIN public.requests_requestevent e ON e.id=d.event_id JOIN public.requests_deliveryline dl ON dl.id=c.delivery_line_id WHERE e.request_id=rid AND (
 f.delivery_id<>dl.delivery_id OR c.effective_before<>dl.handed+COALESCE((SELECT sum(p.effective_after-p.effective_before) FROM public.requests_deliverycorrectionline p JOIN public.requests_deliverycorrection pf ON pf.id=p.correction_id JOIN public.requests_requestdecision px ON px.id=pf.decision_id JOIN public.requests_requestevent pe ON pe.id=px.event_id WHERE p.delivery_line_id=dl.id AND pe.revision<e.revision),0)
 OR e.kind<>'CORRECTED' OR f.previous_id IS DISTINCT FROM (SELECT pf.id FROM public.requests_deliverycorrection pf JOIN public.requests_requestdecision pd ON pd.id=pf.decision_id JOIN public.requests_requestevent pe ON pe.id=pd.event_id WHERE pf.delivery_id=f.delivery_id AND pe.revision<e.revision ORDER BY pe.revision DESC LIMIT 1)
 )) THEN RAISE EXCEPTION 'Delivery correction must continue its original effective chain' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestdecision d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE e.request_id=rid AND (
 d.previous_operator_id IS DISTINCT FROM public.reserve_operator_at(rid,e.revision) OR (e.kind<>'REASSIGNED' AND (SELECT count(*) FROM public.requests_decisionline WHERE decision_id=d.id)<>(SELECT count(*) FROM public.requests_requestline WHERE request_id=rid)) OR (e.kind='REASSIGNED' AND EXISTS(SELECT 1 FROM public.requests_decisionline WHERE decision_id=d.id)) OR (e.kind='CORRECTED' AND NOT EXISTS(SELECT 1 FROM public.requests_deliverycorrectionline cl JOIN public.requests_deliverycorrection cf ON cf.id=cl.correction_id WHERE cf.decision_id=d.id))
 OR (e.kind<>'REASSIGNED' AND d.next_operator_id IS DISTINCT FROM d.previous_operator_id)
 OR (e.kind='CLOSED' AND (d.cause<>'REMAINDER_ABANDONED' OR NOT EXISTS(SELECT 1 FROM public.requests_delivery WHERE request_id=rid)))
 OR (e.kind='CORRECTED' AND NOT EXISTS(SELECT 1 FROM public.requests_deliverycorrection f JOIN public.cases_case c ON c.id=d.case_id WHERE f.decision_id=d.id AND c.delivery_id=f.delivery_id))
 OR (e.kind='RECONCILED' AND d.cause<>'ADMIN_RECONCILIATION') OR (e.kind='RECONCILED' AND NOT EXISTS(SELECT 1 FROM public.cases_stockreconciliation f WHERE f.case_id=d.case_id AND f.operation_id=e.operation_id))
 )) THEN RAISE EXCEPTION 'Invalid administrative origin or assignment' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent e ON e.id=c.event_id JOIN public.requests_requestline l ON l.id=c.line_id JOIN public.inventory_stockentry s ON s.id=c.entry_id WHERE e.request_id=rid AND (l.request_id<>rid OR s.operation_id<>e.operation_id OR s.product_id<>l.product_id OR s.unit<>l.unit
 OR (e.kind='CREATED' AND (s.kind<>'RESERVE' OR s.delta_r<>l.quantity OR s.delta_p<>0))
 OR (e.kind IN ('CLAIMED','REASSIGNED')) OR (e.kind IN ('CANCELLED','REFUSED') AND (s.kind<>'RELEASE' OR s.delta_p<>0 OR s.delta_r>=0))
 OR (e.kind IN ('CORRECTED','REOPENED','CLOSED','RECONCILED') AND NOT EXISTS(SELECT 1 FROM public.requests_decisionline q JOIN public.requests_requestdecision d ON d.id=q.decision_id WHERE q.entry_id=s.id AND q.line_id=l.id AND d.event_id=e.id))
 OR (e.kind='DELIVERED' AND NOT EXISTS(SELECT 1 FROM public.requests_delivery d JOIN public.requests_deliveryline dl ON dl.delivery_id=d.id WHERE d.event_id=e.id AND dl.line_id=l.id AND ((s.kind='DELIVER' AND s.delta_p=-dl.handed AND s.delta_r=-dl.handed AND dl.handed>0) OR (s.kind='RELEASE' AND s.delta_p=0 AND s.delta_r=-dl.missing AND dl.missing>0) OR (s.kind='SHORTAGE' AND s.delta_p=-dl.missing AND s.delta_r=0 AND dl.missing>0))))
 )) THEN RAISE EXCEPTION 'Administrative reservation links must be exact and typed' USING ERRCODE='23514'; END IF;
END; $$;
"""
# Le registre reste unique et toutes les écritures administratives ont une origine typée.
reservations = (
    import_module("apps.requests.migrations.0005_delivery_integrity")
    .SQL.split("CREATE OR REPLACE FUNCTION public.reserve_assert_reservations", 1)[1]
    .split("CREATE FUNCTION public.reserve_delivery_guard", 1)[0]
)
reservations = "CREATE OR REPLACE FUNCTION public.reserve_assert_reservations" + reservations
reservations = reservations.replace(
    "'RESERVE','RELEASE','DELIVER','SHORTAGE'",
    "'RESERVE','RELEASE','DELIVER','SHORTAGE','DELIVERY_CORRECTION'",
)


class Migration(migrations.Migration):
    dependencies = [
        ("requests", "0009_m6_facts"),
        ("cases", "0004_existing_case_projections"),
        ("inventory", "0005_m6_facts"),
    ]
    operations = [migrations.RunSQL(legacy + guard + stockguard + SQL + reservations)]
