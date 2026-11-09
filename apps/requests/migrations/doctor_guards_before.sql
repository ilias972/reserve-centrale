CREATE OR REPLACE FUNCTION public.reserve_assert_request(rid uuid)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
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
END; $function$;

CREATE OR REPLACE FUNCTION public.reserve_assert_request_m5(rid uuid)
 RETURNS void
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
DECLARE r public.requests_internalrequest%ROWTYPE; n bigint; v bigint; cancels bigint; claims bigint; refusals bigint; nd bigint; expected text; anchor uuid;
BEGIN
 SELECT * INTO r FROM public.requests_internalrequest WHERE id=rid; IF NOT FOUND THEN RETURN; END IF;
 SELECT count(*),max(revision),count(*) FILTER(WHERE kind='CANCELLED'),count(*) FILTER(WHERE kind='CLAIMED') ,count(*) FILTER(WHERE kind='REFUSED') INTO n,v,cancels,claims,refusals FROM public.requests_requestevent WHERE request_id=rid;
 SELECT count(*) INTO nd FROM public.requests_delivery WHERE request_id=rid;
 IF n<>v+1 OR r.version<>v OR cancels+refusals>1 OR claims>1 OR (refusals=1 AND claims<>1) OR NOT EXISTS(SELECT 1 FROM public.requests_requestevent WHERE request_id=rid AND kind='CREATED' AND revision=0 AND operation_id=r.creation_operation_id)
 OR NOT EXISTS(SELECT 1 FROM public.requests_requestline WHERE request_id=rid) THEN
  RAISE EXCEPTION 'Request event revisions must form a complete history' USING ERRCODE='23514'; END IF;
 IF (claims=0 AND r.assigned_to_id IS NOT NULL) OR (claims=1 AND NOT EXISTS(SELECT 1 FROM public.requests_requestevent e JOIN public.core_operation o ON o.id=e.operation_id WHERE e.request_id=rid AND e.kind='CLAIMED' AND e.revision=1 AND o.actor_id=r.assigned_to_id)) THEN
  RAISE EXCEPTION 'Assignment requires its unique immutable claim' USING ERRCODE='23514'; END IF;
 IF cancels+refusals=1 THEN
  IF nd<>0 OR r.first_delivery_operation_id IS NOT NULL THEN RAISE EXCEPTION 'Cannot cancel any confirmed delivery' USING ERRCODE='23514'; END IF;
  expected:=CASE WHEN refusals=1 THEN 'REFUSED' ELSE 'CANCELLED' END;
 ELSIF nd=0 THEN expected:=CASE WHEN claims=1 THEN 'PREPARING' ELSE 'NEW' END;
 ELSE
  IF claims<>1 THEN RAISE EXCEPTION 'Delivery requires assignment' USING ERRCODE='23514'; END IF;
  SELECT d.operation_id INTO anchor FROM public.requests_delivery d JOIN public.requests_requestevent e ON e.id=d.event_id WHERE d.request_id=rid ORDER BY e.revision LIMIT 1;
  IF r.first_delivery_operation_id IS DISTINCT FROM anchor THEN RAISE EXCEPTION 'Invalid first delivery anchor' USING ERRCODE='23514'; END IF;
  expected:=CASE WHEN EXISTS(SELECT 1 FROM public.requests_requestline l JOIN public.requests_reservation z ON z.line_id=l.id WHERE l.request_id=rid AND z.remaining>0) THEN 'PARTIAL_OPEN'
   WHEN EXISTS(SELECT 1 FROM public.requests_requestline WHERE request_id=rid AND closed>0) THEN 'PARTIAL_CLOSED' ELSE 'DELIVERED' END;
 END IF;
 IF r.status<>expected OR r.closure_reason<>(CASE WHEN expected='PARTIAL_CLOSED' THEN 'SHORTAGE' WHEN expected='REFUSED' THEN 'REFUSAL' ELSE '' END) OR (nd=0 AND r.first_delivery_operation_id IS NOT NULL) THEN
  RAISE EXCEPTION 'Request state must match immutable facts' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestline l LEFT JOIN public.requests_reservation z ON z.line_id=l.id WHERE l.request_id=rid AND (
  z.line_id IS NULL OR z.product_id<>l.product_id OR l.quantity<>l.delivered+l.closed+z.remaining
  OR l.delivered<>COALESCE((SELECT sum(handed) FROM public.requests_deliveryline dl WHERE dl.line_id=l.id),0)
  OR l.closed<>CASE WHEN cancels+refusals=1 THEN l.quantity ELSE COALESCE((SELECT sum(missing) FROM public.requests_deliveryline dl WHERE dl.line_id=l.id),0) END
  OR z.remaining<>COALESCE((SELECT sum(e.delta_r) FROM public.requests_reservationchange c JOIN public.inventory_stockentry e ON e.id=c.entry_id WHERE c.line_id=l.id),0)
  OR z.version<>(SELECT count(DISTINCT c.event_id)-1 FROM public.requests_reservationchange c WHERE c.line_id=l.id)
  OR NOT EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent e ON e.id=c.event_id JOIN public.inventory_stockentry s ON s.id=c.entry_id WHERE c.line_id=l.id AND e.kind='CREATED' AND s.delta_r=l.quantity)
 )) THEN RAISE EXCEPTION 'Q=L+C+U and line projections must match delivery and reservation facts' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_requestevent e JOIN public.core_operation o ON o.id=e.operation_id JOIN public.core_operationintent i ON i.id=o.intent_id WHERE e.request_id=rid AND (
  i.state<>'CONFIRMED'
  OR (e.kind IN ('CREATED','CANCELLED') AND (o.actor_id<>r.assistant_id OR o.actor_role<>'ASSISTANT'))
  OR (e.kind IN ('CLAIMED','DELIVERED','REFUSED') AND (o.actor_id<>r.assigned_to_id OR o.actor_role<>'REPLENISHER'))
  OR o.command_type<>CASE e.kind WHEN 'CREATED' THEN 'requests.create' WHEN 'CANCELLED' THEN 'requests.cancel' WHEN 'CLAIMED' THEN 'requests.claim' WHEN 'REFUSED' THEN 'requests.refuse' ELSE 'requests.deliver' END
  OR (e.kind<>'CREATED' AND o.resource_id IS DISTINCT FROM rid)
  OR (e.kind='CREATED' AND e.revision<>0)
  OR (e.kind IN ('CANCELLED','REFUSED') AND e.revision<>r.version)
  OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=o.id AND b.aggregate_id=rid AND b.event_type=CASE e.kind WHEN 'CREATED' THEN 'request.created' WHEN 'CANCELLED' THEN 'request.cancelled' WHEN 'CLAIMED' THEN 'request.claimed' WHEN 'REFUSED' THEN 'request.refused' ELSE 'request.delivered' END)
  OR (e.kind='DELIVERED' AND NOT EXISTS(SELECT 1 FROM public.requests_delivery d WHERE d.event_id=e.id AND d.operation_id=e.operation_id AND d.request_id=rid))
 )) THEN RAISE EXCEPTION 'Request event requires matching confirmed operation and outbox' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.requests_reservationchange c JOIN public.requests_requestevent a ON a.id=c.event_id JOIN public.requests_requestline l ON l.id=c.line_id JOIN public.inventory_stockentry e ON e.id=c.entry_id
  WHERE (a.request_id=rid OR l.request_id=rid) AND (a.request_id<>l.request_id OR e.operation_id<>a.operation_id OR e.product_id<>l.product_id OR e.unit<>l.unit
  OR (a.kind='CREATED' AND (e.kind<>'RESERVE' OR e.delta_p<>0 OR e.delta_r<>l.quantity))
  OR (a.kind IN ('CANCELLED','REFUSED') AND (e.kind<>'RELEASE' OR e.delta_p<>0 OR e.delta_r<>-l.quantity))
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
END; $function$;

CREATE OR REPLACE FUNCTION public.reserve_request_guard()
 RETURNS trigger
 LANGUAGE plpgsql
 SET search_path TO 'pg_catalog'
AS $function$
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
   WHERE o.id=op AND i.state='READY' AND ((o.actor_role='ASSISTANT' AND o.command_type IN ('requests.create','requests.cancel')) OR (o.actor_role='REPLENISHER' AND o.command_type IN ('requests.claim','requests.deliver','requests.refuse')) OR (o.actor_role='ADMIN' AND o.command_type IN ('corrections.confirm','requests.close_remainder','requests.reopen','requests.reassign','stock_reconciliations.confirm')))) THEN
  RAISE EXCEPTION 'Request facts require a new authorized confirmation' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $function$;
