"""Contraintes des faits M8, équations fournisseurs et provenance des entrées."""

from django.db import migrations

SQL = r"""
CREATE OR REPLACE FUNCTION public.reserve_m7_line_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE p public.catalog_product%ROWTYPE;
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Purchase line is immutable' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
  IF (to_jsonb(NEW)-ARRAY['cancelled','received']) IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['cancelled','received']) THEN RAISE EXCEPTION 'Initial ordered quantities are immutable' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT * INTO p FROM public.catalog_product WHERE id=NEW.product_id;
  IF NEW.product_code<>p.code OR NEW.product_name<>p.name OR NEW.unit<>p.unit OR NEW.product_version<>p.version OR NEW.photo_id IS DISTINCT FROM p.current_photo_id
  OR NOT EXISTS(SELECT 1 FROM public.purchasing_purchaseorder po JOIN public.core_operation o ON o.id=po.creation_operation_id JOIN public.core_operationintent i ON i.id=o.intent_id WHERE po.id=NEW.order_id AND i.state='READY')
  THEN RAISE EXCEPTION 'Invalid purchase line snapshot or terminal purchase' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE OR REPLACE FUNCTION public.reserve_m7_assert_order(oid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE po public.purchasing_purchaseorder%ROWTYPE; key jsonb; n bigint; v bigint; distinct_n bigint; expected text;
BEGIN
 SELECT * INTO po FROM public.purchasing_purchaseorder WHERE id=oid;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing purchase' USING ERRCODE='23514'; END IF;
 SELECT jsonb_agg(jsonb_build_array(product_id::text,quantity) ORDER BY product_id) INTO key FROM public.purchasing_orderline WHERE order_id=oid;
 SELECT count(*),coalesce(max(revision),0),count(DISTINCT revision) INTO n,v,distinct_n FROM (
 SELECT revision FROM public.purchasing_remainderevent WHERE order_id=oid UNION ALL SELECT revision FROM public.receiving_orderevent WHERE order_id=oid) s;
 IF key IS NULL OR key<>po.lines_key OR po.version<>n OR n<>v OR distinct_n<>n OR NOT public.reserve_m7_operation(po.creation_operation_id,'orders.create',NULL) THEN RAISE EXCEPTION 'Invalid purchase origin or order event chain' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchasing_orderline l WHERE l.order_id=oid AND (
  l.received<>(public.reserve_m8_line_before(l.id,po.version+1)->>'F')::bigint OR l.cancelled<>(public.reserve_m8_line_before(l.id,po.version+1)->>'A')::bigint
 )) THEN RAISE EXCEPTION 'Supplier projections must match immutable receipt and remainder facts' USING ERRCODE='23514'; END IF;
 expected:=CASE WHEN EXISTS(SELECT 1 FROM public.purchasing_orderline WHERE order_id=oid AND quantity>received+cancelled)
 THEN CASE WHEN EXISTS(SELECT 1 FROM public.purchasing_orderline WHERE order_id=oid AND received>0) THEN 'PARTIAL' ELSE 'WAITING' END
 WHEN EXISTS(SELECT 1 FROM public.purchasing_orderline WHERE order_id=oid AND cancelled>0)
 THEN CASE WHEN EXISTS(SELECT 1 FROM public.purchasing_orderline WHERE order_id=oid AND received>0) THEN 'CLOSED_REMAINDER_CANCELLED' ELSE 'CANCELLED_NO_RECEIPT' END ELSE 'RECEIVED' END;
 IF po.status<>expected THEN RAISE EXCEPTION 'Invalid supplier order status' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.audit_auditevent a WHERE a.operation_id=po.creation_operation_id AND a.after->>'id'=oid::text AND a.after->>'supplier_name'=po.supplier_name AND a.after->>'purchased_on'=po.purchased_on::text AND a.after->>'external_reference'=po.external_reference)
 OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=po.creation_operation_id AND b.event_type='purchase.recorded' AND b.aggregate_id=oid AND b.revision=1 AND b.payload->'order'->>'id'=oid::text AND b.payload->'recipient_roles'='["ADMIN","ACCOUNTANT_EXTERNAL"]'::jsonb) THEN RAISE EXCEPTION 'Purchase audit or accounting event missing' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchasing_remainderevent e WHERE e.order_id=oid AND (
  NOT public.reserve_m7_operation(e.operation_id,'orders.cancel_remainder',oid)
  OR NOT EXISTS(SELECT 1 FROM public.purchasing_remainderline c WHERE c.event_id=e.id)
  OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=e.operation_id AND b.event_type='purchase.remainder_cancelled' AND b.aggregate_id=oid AND b.revision=e.revision)
 )) THEN RAISE EXCEPTION 'Invalid cancellation' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.purchasing_remainderline c JOIN public.purchasing_remainderevent e ON e.id=c.event_id JOIN public.purchasing_orderline l ON l.id=c.line_id WHERE e.order_id=oid AND (
 l.order_id<>oid OR c.cancelled_before<>(public.reserve_m8_line_before(l.id,e.revision)->>'A')::bigint OR c.quantity>(public.reserve_m8_line_before(l.id,e.revision)->>'W')::bigint
 )) THEN RAISE EXCEPTION 'Cancellation exceeds real outstanding quantity' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_orderevent e LEFT JOIN public.receiving_receipt r ON r.event_id=e.id LEFT JOIN public.receiving_reinstatement a ON a.event_id=e.id LEFT JOIN public.receiving_receiptcorrection c ON c.event_id=e.id WHERE e.order_id=oid AND (
  (e.kind='RECEIPT' AND (r.id IS NULL OR NOT public.reserve_m8_operation(e.operation_id,'receipts.create',oid,'REPLENISHER')))
  OR (e.kind='REINSTATE' AND (a.id IS NULL OR NOT public.reserve_m8_operation(e.operation_id,'orders.reinstate_remainder',oid,'ADMIN')))
  OR (e.kind='CORRECTION' AND (c.id IS NULL OR NOT public.reserve_m8_operation(e.operation_id,'corrections.confirm',c.receipt_id,'ADMIN')))
  OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent b WHERE b.operation_id=e.operation_id AND b.aggregate_id=oid AND b.revision=e.revision AND b.event_type=CASE e.kind WHEN 'RECEIPT' THEN 'purchase.received' WHEN 'REINSTATE' THEN 'purchase.remainder_reinstated' ELSE 'purchase.receipt_corrected' END)
 )) THEN RAISE EXCEPTION 'Order event requires its exact confirmed receipt, reinstatement or correction' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_reinstatement h JOIN public.receiving_orderevent e ON e.id=h.event_id JOIN public.receiving_arrivalreport report ON report.id=h.report_id WHERE e.order_id=oid AND (report.kind NOT IN ('UNKNOWN_ORDER','LATE_CANCELLED_DELIVERY') OR NOT h.external_verification OR NOT EXISTS(SELECT 1 FROM public.receiving_reinstatementline WHERE reinstatement_id=h.id))) THEN RAISE EXCEPTION 'Empty or unverified reinstatement' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_reinstatementline a JOIN public.receiving_reinstatement h ON h.id=a.reinstatement_id JOIN public.receiving_orderevent e ON e.id=h.event_id JOIN public.purchasing_orderline l ON l.id=a.line_id WHERE e.order_id=oid AND (
  l.order_id<>oid OR a.cancelled_before<>(public.reserve_m8_line_before(l.id,e.revision)->>'A')::bigint OR a.quantity>a.cancelled_before
  OR public.reserve_m8_item_state(a.item_id)->>'report_id' IS DISTINCT FROM h.report_id::text
  OR public.reserve_m8_item_state(a.item_id)->>'order_line_id' IS DISTINCT FROM a.line_id::text
 )) THEN RAISE EXCEPTION 'Reinstatement requires its isolated goods and cancelled commitment' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_m8_assert_item(iid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE i public.receiving_quarantineitem%ROWTYPE; s jsonb; initial_quantity bigint; source_op uuid; source_case uuid;
BEGIN
 SELECT * INTO i FROM public.receiving_quarantineitem WHERE id=iid;
 s:=public.reserve_m8_item_state(iid);
 IF i.id IS NULL OR s IS NULL THEN RAISE EXCEPTION 'Missing typed quarantine origin' USING ERRCODE='23514'; END IF;
 IF i.receipt_line_id IS NOT NULL THEN
  SELECT CASE WHEN i.kind='SURPLUS' THEN l.surplus ELSE l.nonconforming END,e.operation_id INTO initial_quantity,source_op FROM public.receiving_receiptline l JOIN public.receiving_receipt r ON r.id=l.receipt_id JOIN public.receiving_orderevent e ON e.id=r.event_id WHERE l.id=i.receipt_line_id;
  IF NOT EXISTS(SELECT 1 FROM public.cases_case c JOIN public.receiving_receiptline l ON l.receipt_id=c.receipt_id WHERE c.id=i.case_id AND l.id=i.receipt_line_id AND c.kind=CASE WHEN i.kind='SURPLUS' THEN 'SURPLUS' ELSE 'NONCONFORMITY' END) THEN RAISE EXCEPTION 'Receipt isolation requires matching case' USING ERRCODE='23514'; END IF;
  IF i.creation_operation_id<>source_op AND NOT EXISTS(SELECT 1 FROM public.receiving_receiptcorrectionline cl JOIN public.receiving_receiptcorrection h ON h.id=cl.correction_id JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE cl.line_id=i.receipt_line_id AND e.operation_id=i.creation_operation_id AND CASE WHEN i.kind='SURPLUS' THEN cl.surplus_after ELSE cl.nonconforming_after END>0) THEN RAISE EXCEPTION 'Isolation needs its receipt or explicit correction' USING ERRCODE='23514'; END IF;
 ELSIF i.report_line_id IS NOT NULL THEN
  SELECT l.quantity,h.operation_id,h.case_id INTO initial_quantity,source_op,source_case FROM public.receiving_reportline l JOIN public.receiving_arrivalreport h ON h.id=l.report_id WHERE l.id=i.report_line_id AND ((i.kind='SURPLUS' AND h.kind='SURPLUS') OR (i.kind='LATE_CANCELLED' AND h.kind='LATE_CANCELLED_DELIVERY'));
  IF source_case IS DISTINCT FROM i.case_id OR i.creation_operation_id IS DISTINCT FROM source_op THEN RAISE EXCEPTION 'Arrival isolation needs its original report' USING ERRCODE='23514'; END IF;
 ELSE
  SELECT l.quantity,h.operation_id,r.case_id INTO initial_quantity,source_op,source_case FROM public.receiving_unknownlinkline l JOIN public.receiving_unknownlink h ON h.report_id=l.link_id JOIN public.receiving_arrivalreport r ON r.id=h.report_id WHERE l.id=i.link_line_id;
  IF source_case IS DISTINCT FROM i.case_id OR i.creation_operation_id IS DISTINCT FROM source_op THEN RAISE EXCEPTION 'Unknown isolation requires its verified allocation' USING ERRCODE='23514'; END IF;
 END IF;
 IF initial_quantity IS DISTINCT FROM i.quantity OR (s->>'remaining')::bigint<0 OR (s->>'grant_remaining')::bigint<0 OR (s->>'grant_remaining')::bigint>(s->>'remaining')::bigint OR (s->>'granted')::bigint>i.quantity THEN RAISE EXCEPTION 'Quarantine quantities or grants consumed twice' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_quarantinedecision d WHERE d.item_id=iid AND (
 NOT public.reserve_m8_operation(d.operation_id,CASE WHEN d.kind='ACCEPT' THEN 'quarantine.accept_surplus' WHEN d.kind='REFUSE' THEN 'quarantine.refuse' ELSE 'receiving.case_resolve' END,CASE WHEN d.kind='EXTERNAL' THEN i.case_id ELSE i.id END,'ADMIN')
 OR (d.kind='ACCEPT' AND (i.kind<>'SURPLUS' OR NOT EXISTS(SELECT 1 FROM public.inventory_stockentry e WHERE e.id=d.entry_id AND e.operation_id=d.operation_id AND e.kind='SURPLUS_ACCEPT' AND e.product_id=(s->>'product_id')::uuid AND e.delta_p=d.quantity AND e.delta_r=0)))
 OR (d.kind<>'ACCEPT' AND d.entry_id IS NOT NULL)
 )) THEN RAISE EXCEPTION 'Isolation decision lacks its authorized fact or stock origin' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_sourceconsumption c JOIN public.receiving_receiptline l ON l.id=c.line_id JOIN public.receiving_receipt h ON h.id=l.receipt_id JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE c.item_id=iid AND (
  i.kind NOT IN ('UNKNOWN_ORDER','LATE_CANCELLED') OR h.source_report_id::text IS DISTINCT FROM s->>'report_id'
  OR l.order_line_id::text IS DISTINCT FROM s->>'order_line_id' OR c.quantity<>l.conforming+l.nonconforming
  OR c.reinstated_quantity>c.quantity OR (i.kind='LATE_CANCELLED' AND (public.reserve_m8_effective(l.id)->>'source_grant')::bigint<>(public.reserve_m8_effective(l.id)->>'conforming')::bigint+(public.reserve_m8_effective(l.id)->>'nonconforming')::bigint)
 )) THEN RAISE EXCEPTION 'Receipt must consume only its own identified isolated goods' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_m8_assert_receipt(rid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE r public.receiving_receipt%ROWTYPE; e public.receiving_orderevent%ROWTYPE; n bigint; v bigint;
BEGIN
 SELECT * INTO r FROM public.receiving_receipt WHERE id=rid;
 SELECT * INTO e FROM public.receiving_orderevent WHERE id=r.event_id;
 SELECT count(*),coalesce(max(revision),0) INTO n,v FROM public.receiving_receiptcorrection WHERE receipt_id=rid;
 IF r.id IS NULL OR e.kind<>'RECEIPT' OR r.reference!~'^REC-[0-9]{8,}$' OR r.version<>n OR n<>v OR NOT EXISTS(SELECT 1 FROM public.receiving_receiptline WHERE receipt_id=rid) THEN RAISE EXCEPTION 'Receipt requires its immutable arrival lines and correction chain' USING ERRCODE='23514'; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.purchasing_orderline l WHERE l.order_id=e.order_id AND (public.reserve_m8_line_before(l.id,e.revision)->>'W')::bigint>0) THEN RAISE EXCEPTION 'Closed purchase cannot receive normally' USING ERRCODE='23514'; END IF;
 IF r.source_report_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.receiving_arrivalreport h LEFT JOIN public.receiving_unknownlink u ON u.report_id=h.id WHERE h.id=r.source_report_id AND ((h.kind='UNKNOWN_ORDER' AND u.order_id=e.order_id) OR (h.kind='LATE_CANCELLED_DELIVERY' AND h.order_id=e.order_id))) THEN RAISE EXCEPTION 'Receipt source report belongs to another order' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_receiptline l JOIN public.purchasing_orderline ol ON ol.id=l.order_line_id WHERE l.receipt_id=rid AND (
  ol.order_id<>e.order_id OR l.conforming+l.nonconforming=0 OR l.waiting_before<>(public.reserve_m8_line_before(ol.id,e.revision)->>'W')::bigint
  OR l.regular<>least(l.conforming,l.waiting_before)
  OR (l.conforming>l.waiting_before AND (public.reserve_m8_line_before(ol.id,e.revision)->>'A')::bigint>0)
  OR (l.regular>0 AND NOT EXISTS(SELECT 1 FROM public.inventory_stockentry s WHERE s.id=l.entry_id AND s.operation_id=e.operation_id AND s.product_id=ol.product_id AND s.kind='RECEIPT_REGULAR' AND s.delta_p=l.regular AND s.delta_r=0 AND s.unit=ol.unit)) OR (l.regular=0 AND l.entry_id IS NOT NULL)
  OR ((r.source_report_id IS NOT NULL) IS DISTINCT FROM EXISTS(SELECT 1 FROM public.receiving_sourceconsumption WHERE line_id=l.id))
  OR (l.surplus>0 AND NOT EXISTS(SELECT 1 FROM public.receiving_quarantineitem WHERE receipt_line_id=l.id AND kind='SURPLUS'))
  OR (l.nonconforming>0 AND NOT EXISTS(SELECT 1 FROM public.receiving_quarantineitem WHERE receipt_line_id=l.id AND kind='NONCONFORMING'))
 )) THEN RAISE EXCEPTION 'Receipt allocation, stock provenance or isolation inconsistent' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_receiptcorrection h JOIN public.receiving_orderevent ce ON ce.id=h.event_id JOIN public.cases_case c ON c.id=h.case_id WHERE h.receipt_id=rid AND (
 ce.kind<>'CORRECTION' OR ce.order_id<>e.order_id OR c.receipt_id<>rid OR c.kind<>'RECEIPT_ERROR'
 OR h.previous_id IS DISTINCT FROM (SELECT p.id FROM public.receiving_receiptcorrection p WHERE p.receipt_id=rid AND p.revision<h.revision ORDER BY p.revision DESC LIMIT 1)
 OR NOT EXISTS(SELECT 1 FROM public.receiving_receiptcorrectionline WHERE correction_id=h.id)
 )) THEN RAISE EXCEPTION 'Invalid correction origin or previous correction' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_receiptcorrectionline c JOIN public.receiving_receiptcorrection h ON h.id=c.correction_id JOIN public.receiving_orderevent ce ON ce.id=h.event_id LEFT JOIN public.receiving_receiptcorrectionline prior ON prior.id=(SELECT pc.id FROM public.receiving_receiptcorrectionline pc JOIN public.receiving_receiptcorrection ph ON ph.id=pc.correction_id WHERE pc.line_id=c.line_id AND ph.revision<h.revision ORDER BY ph.revision DESC LIMIT 1) JOIN public.receiving_receiptline l ON l.id=c.line_id JOIN public.purchasing_orderline ol ON ol.id=l.order_line_id LEFT JOIN public.receiving_sourceconsumption sc ON sc.line_id=l.id WHERE h.receipt_id=rid AND (
  l.receipt_id<>rid OR c.regular_before<>coalesce(prior.regular_after,l.regular) OR c.surplus_before<>coalesce(prior.surplus_after,l.surplus) OR c.conforming_before<>coalesce(prior.conforming_after,l.conforming) OR c.nonconforming_before<>coalesce(prior.nonconforming_after,l.nonconforming) OR c.nonconformity_before<>coalesce(prior.nonconformity_after,l.nonconformity)
  OR c.source_grant_before<>coalesce(prior.source_grant_after,sc.reinstated_quantity,0)
  OR c.conforming_after+c.nonconforming_after>1000000000
  OR c.regular_after<>(CASE WHEN c.conforming_after>c.conforming_before THEN c.regular_before+least(c.conforming_after-c.conforming_before,(public.reserve_m8_line_before(ol.id,ce.revision)->>'W')::bigint) ELSE least(c.regular_before,c.conforming_after) END)
  OR (c.conforming_after-c.conforming_before>(public.reserve_m8_line_before(ol.id,ce.revision)->>'W')::bigint AND (public.reserve_m8_line_before(ol.id,ce.revision)->>'A')::bigint>0)
  OR c.regular_after-c.regular_before>(public.reserve_m8_line_before(ol.id,ce.revision)->>'W')::bigint
  OR ((c.regular_after<>c.regular_before) AND NOT EXISTS(SELECT 1 FROM public.inventory_stockentry s WHERE s.id=c.entry_id AND s.operation_id=ce.operation_id AND s.product_id=ol.product_id AND s.kind='RECEIPT_CORRECTION' AND s.delta_p=c.regular_after-c.regular_before AND s.delta_r=0 AND s.unit=ol.unit))
  OR (c.regular_after=c.regular_before AND c.entry_id IS NOT NULL)
  OR c.source_grant_after>c.conforming_after+c.nonconforming_after OR (sc.id IS NULL AND (c.source_grant_after<>0 OR c.source_grant_before<>0))
 )) THEN RAISE EXCEPTION 'Correction must start from effective facts and match its signed stock delta' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_m8_assert_report(hid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE h public.receiving_arrivalreport%ROWTYPE; o public.core_operation%ROWTYPE;
BEGIN
 SELECT * INTO h FROM public.receiving_arrivalreport WHERE id=hid;
 SELECT * INTO o FROM public.core_operation WHERE id=h.operation_id;
 IF h.id IS NULL OR h.reference!~'^ISO-[0-9]{8,}$' OR NOT public.reserve_m8_operation(h.operation_id,CASE h.kind WHEN 'UNKNOWN_ORDER' THEN 'receiving.unknown' WHEN 'SURPLUS' THEN 'receiving.extra' ELSE 'receiving.late' END,h.order_id,'REPLENISHER') THEN RAISE EXCEPTION 'Report requires its exact operator declaration' USING ERRCODE='23514'; END IF;
 IF (h.kind='UNKNOWN_ORDER' AND h.order_version IS NOT NULL) OR (h.kind<>'UNKNOWN_ORDER' AND h.order_version IS NULL) THEN RAISE EXCEPTION 'Report must retain the observed order version' USING ERRCODE='23514'; END IF;
 IF h.kind<>'UNKNOWN_ORDER' AND NOT EXISTS(SELECT 1 FROM public.receiving_reportline WHERE report_id=hid) THEN RAISE EXCEPTION 'Known order report needs observed quantities' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_reportline l LEFT JOIN public.purchasing_orderline ol ON ol.id=l.order_line_id WHERE l.report_id=hid AND ((h.kind<>'UNKNOWN_ORDER' AND (ol.order_id IS DISTINCT FROM h.order_id OR l.product_id IS DISTINCT FROM ol.product_id OR l.unit<>ol.unit OR l.product_name<>ol.product_name OR l.quantity IS NULL OR (h.kind='LATE_CANCELLED_DELIVERY' AND (public.reserve_m8_line_before(ol.id,h.order_version+1)->>'A')::bigint=0) OR (h.kind='SURPLUS' AND ((public.reserve_m8_line_before(ol.id,h.order_version+1)->>'A')::bigint>0 OR (public.reserve_m8_line_before(ol.id,h.order_version+1)->>'W')::bigint>0)) OR NOT EXISTS(SELECT 1 FROM public.receiving_quarantineitem i WHERE i.report_line_id=l.id))) OR (h.kind='UNKNOWN_ORDER' AND l.order_line_id IS NOT NULL))) THEN RAISE EXCEPTION 'Report lines need their real order and isolated goods' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_quarantinedecision d WHERE d.report_id=hid AND (NOT public.reserve_m8_operation(d.operation_id,'receiving.case_resolve',h.case_id,'ADMIN') OR EXISTS(SELECT 1 FROM public.receiving_unknownlink WHERE report_id=hid))) THEN RAISE EXCEPTION 'Disposed parcel cannot be linked or received' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_unknownlink u WHERE u.report_id=hid AND (
 h.kind<>'UNKNOWN_ORDER' OR NOT u.verification OR NOT public.reserve_m8_operation(u.operation_id,'cases.link_order',h.case_id,'ADMIN') OR NOT EXISTS(SELECT 1 FROM public.receiving_unknownlinkline WHERE link_id=u.report_id)
 )) THEN RAISE EXCEPTION 'Unknown parcel link must be verified and nonempty' USING ERRCODE='23514'; END IF;
 IF EXISTS(SELECT 1 FROM public.receiving_unknownlinkline l JOIN public.receiving_unknownlink u ON u.report_id=l.link_id JOIN public.purchasing_orderline ol ON ol.id=l.order_line_id WHERE u.report_id=hid AND (ol.order_id<>u.order_id OR NOT EXISTS(SELECT 1 FROM public.receiving_quarantineitem i WHERE i.link_line_id=l.id))) THEN RAISE EXCEPTION 'Verified parcel quantities must be isolated on one order' USING ERRCODE='23514'; END IF;
END; $$;

CREATE FUNCTION public.reserve_m8_fact_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE op uuid; cmd text; role text; s jsonb; r public.receiving_receiptline%ROWTYPE; item public.receiving_quarantineitem%ROWTYPE; prior uuid; ord uuid;
BEGIN
 IF TG_TABLE_NAME IN ('receiving_orderevent','receiving_arrivalreport','receiving_unknownlink','receiving_manualreminder','receiving_quarantinedecision') THEN op:=NEW.operation_id;
 ELSIF TG_TABLE_NAME='receiving_receipt' THEN SELECT operation_id INTO op FROM public.receiving_orderevent WHERE id=NEW.event_id;
 ELSIF TG_TABLE_NAME='receiving_receiptline' THEN SELECT e.operation_id INTO op FROM public.receiving_receipt h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE h.id=NEW.receipt_id;
 ELSIF TG_TABLE_NAME='receiving_reportline' THEN SELECT operation_id INTO op FROM public.receiving_arrivalreport WHERE id=NEW.report_id;
 ELSIF TG_TABLE_NAME='receiving_unknownlinkline' THEN SELECT operation_id INTO op FROM public.receiving_unknownlink WHERE report_id=NEW.link_id;
 ELSIF TG_TABLE_NAME='receiving_quarantineitem' THEN op:=NEW.creation_operation_id;
 ELSIF TG_TABLE_NAME IN ('receiving_reinstatement','receiving_receiptcorrection') THEN SELECT operation_id INTO op FROM public.receiving_orderevent WHERE id=NEW.event_id;
 ELSIF TG_TABLE_NAME='receiving_reinstatementline' THEN SELECT e.operation_id INTO op FROM public.receiving_reinstatement h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE h.id=NEW.reinstatement_id;
 ELSIF TG_TABLE_NAME='receiving_receiptcorrectionline' THEN SELECT e.operation_id INTO op FROM public.receiving_receiptcorrection h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE h.id=NEW.correction_id;
 ELSE SELECT e.operation_id INTO op FROM public.receiving_receiptline l JOIN public.receiving_receipt h ON h.id=l.receipt_id JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE l.id=NEW.line_id; END IF;
 SELECT o.command_type,o.actor_role INTO cmd,role FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.accounts_user u ON u.id=o.actor_id WHERE o.id=op AND i.state='READY' AND u.is_active AND NOT u.must_change_password AND u.role=o.actor_role;
 IF cmd IS NULL OR NOT ((role='REPLENISHER' AND cmd IN ('receipts.create','receiving.unknown','receiving.late','receiving.extra','orders.remind')) OR (role='ADMIN' AND cmd IN ('quarantine.accept_surplus','quarantine.refuse','receiving.case_resolve','cases.link_order','orders.reinstate_remainder','corrections.confirm'))) THEN RAISE EXCEPTION 'Receiving fact requires a new authorized operation' USING ERRCODE='23514'; END IF;
 IF TG_TABLE_NAME='receiving_orderevent' THEN
  IF NEW.revision<>(SELECT version+1 FROM public.purchasing_purchaseorder WHERE id=NEW.order_id) OR (NEW.kind='RECEIPT' AND cmd<>'receipts.create') OR (NEW.kind='REINSTATE' AND cmd<>'orders.reinstate_remainder') OR (NEW.kind='CORRECTION' AND cmd<>'corrections.confirm') THEN RAISE EXCEPTION 'Wrong order event version or action' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='receiving_quarantinedecision' THEN
  IF role<>'ADMIN' OR NOT NEW.verification THEN RAISE EXCEPTION 'Decision is administrator only' USING ERRCODE='23514'; END IF;
  IF NEW.item_id IS NOT NULL THEN
   SELECT * INTO item FROM public.receiving_quarantineitem WHERE id=NEW.item_id FOR UPDATE;
   s:=public.reserve_m8_item_state(NEW.item_id);
   IF NEW.version_before IS DISTINCT FROM (s->>'version')::bigint OR NEW.quantity IS DISTINCT FROM (s->>'remaining')::bigint OR NEW.remaining_before IS DISTINCT FROM NEW.quantity OR NEW.quantity<=0 OR (NEW.kind IN ('ACCEPT','REFUSE') AND (item.kind<>'SURPLUS' OR (s->>'refused')::boolean)) OR NEW.grant_released<>(CASE WHEN NEW.kind='EXTERNAL' THEN (s->>'grant_remaining')::bigint ELSE 0 END) THEN RAISE EXCEPTION 'Decision must apply to entire actual remaining isolation' USING ERRCODE='23514'; END IF;
  ELSE
   IF NOT EXISTS(SELECT 1 FROM public.receiving_arrivalreport h JOIN public.cases_case c ON c.id=h.case_id WHERE h.id=NEW.report_id AND h.kind='UNKNOWN_ORDER' AND c.version=NEW.version_before AND c.status<>'RESOLVED' AND NOT EXISTS(SELECT 1 FROM public.receiving_unknownlink WHERE report_id=h.id)) THEN RAISE EXCEPTION 'Whole unidentified parcel external treatment required' USING ERRCODE='23514'; END IF;
  END IF;
 ELSIF TG_TABLE_NAME='receiving_sourceconsumption' THEN
  s:=public.reserve_m8_item_state(NEW.item_id); SELECT * INTO r FROM public.receiving_receiptline WHERE id=NEW.line_id;
  IF NEW.quantity<>r.conforming+r.nonconforming OR NEW.quantity>(s->>'remaining')::bigint OR NEW.reinstated_quantity<>least(NEW.quantity,(s->>'grant_remaining')::bigint) OR ((s->>'kind')='LATE_CANCELLED' AND NEW.reinstated_quantity<>NEW.quantity) THEN RAISE EXCEPTION 'Isolation already consumed or reinstatement missing' USING ERRCODE='23514'; END IF;
 ELSIF TG_TABLE_NAME='receiving_manualreminder' THEN
  PERFORM id FROM public.purchasing_purchaseorder WHERE id=NEW.order_id FOR UPDATE;
  SELECT id INTO prior FROM public.receiving_manualreminder WHERE order_id=NEW.order_id ORDER BY (SELECT confirmed_at FROM public.core_operation WHERE id=operation_id) DESC,id DESC LIMIT 1;
  IF cmd<>'orders.remind' OR NOT EXISTS(SELECT 1 FROM public.core_operation WHERE id=op AND resource_id=NEW.order_id) OR NEW.previous_id IS DISTINCT FROM prior OR NOT EXISTS(SELECT 1 FROM public.purchasing_purchaseorder WHERE id=NEW.order_id AND status='PARTIAL') THEN RAISE EXCEPTION 'Manual reminder must follow current partial-order reminder' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION public.reserve_m8_receipt_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND (to_jsonb(NEW)-'version') IS DISTINCT FROM (to_jsonb(OLD)-'version')) THEN RAISE EXCEPTION 'Confirmed receipt origin is immutable' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END; $$;

CREATE FUNCTION public.reserve_m8_entry_check() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF NEW.kind='RECEIPT_REGULAR' AND NOT EXISTS(SELECT 1 FROM public.receiving_receiptline l JOIN public.receiving_receipt r ON r.id=l.receipt_id JOIN public.receiving_orderevent e ON e.id=r.event_id WHERE l.entry_id=NEW.id AND e.operation_id=NEW.operation_id AND l.regular=NEW.delta_p AND public.reserve_m8_operation(NEW.operation_id,'receipts.create',e.order_id,'REPLENISHER')) THEN RAISE EXCEPTION 'Regular receipt entry requires its typed arrival' USING ERRCODE='23514'; END IF;
 IF NEW.kind='SURPLUS_ACCEPT' AND NOT EXISTS(SELECT 1 FROM public.receiving_quarantinedecision d WHERE d.entry_id=NEW.id AND d.kind='ACCEPT' AND d.quantity=NEW.delta_p AND public.reserve_m8_operation(NEW.operation_id,'quarantine.accept_surplus',d.item_id,'ADMIN')) THEN RAISE EXCEPTION 'Surplus entry requires its exact authorized decision' USING ERRCODE='23514'; END IF;
 IF NEW.kind='RECEIPT_CORRECTION' AND NOT EXISTS(SELECT 1 FROM public.receiving_receiptcorrectionline l JOIN public.receiving_receiptcorrection h ON h.id=l.correction_id JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE l.entry_id=NEW.id AND l.regular_after-l.regular_before=NEW.delta_p AND public.reserve_m8_operation(NEW.operation_id,'corrections.confirm',h.receipt_id,'ADMIN')) THEN RAISE EXCEPTION 'Correction entry requires its original receipt and corrective fact' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER m8_entry_check AFTER INSERT ON public.inventory_stockentry DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m8_entry_check();

CREATE FUNCTION public.reserve_m8_check() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE oid uuid; rid uuid; hid uuid; iid uuid; s jsonb; fact_op uuid;
BEGIN
 IF TG_TABLE_NAME='receiving_orderevent' THEN oid:=NEW.order_id; fact_op:=NEW.operation_id;
 ELSIF TG_TABLE_NAME='receiving_receipt' THEN rid:=NEW.id;
 ELSIF TG_TABLE_NAME='receiving_receiptline' THEN rid:=NEW.receipt_id;
 ELSIF TG_TABLE_NAME='receiving_arrivalreport' THEN hid:=NEW.id; fact_op:=NEW.operation_id;
 ELSIF TG_TABLE_NAME='receiving_reportline' THEN hid:=NEW.report_id;
 ELSIF TG_TABLE_NAME='receiving_unknownlink' THEN hid:=NEW.report_id;fact_op:=NEW.operation_id;
 ELSIF TG_TABLE_NAME='receiving_unknownlinkline' THEN hid:=NEW.link_id;
 ELSIF TG_TABLE_NAME='receiving_quarantineitem' THEN iid:=NEW.id;fact_op:=NEW.creation_operation_id;
 ELSIF TG_TABLE_NAME='receiving_quarantinedecision' THEN iid:=NEW.item_id;hid:=NEW.report_id;fact_op:=NEW.operation_id;
 ELSIF TG_TABLE_NAME='receiving_sourceconsumption' THEN iid:=NEW.item_id;SELECT receipt_id INTO rid FROM public.receiving_receiptline WHERE id=NEW.line_id;
 ELSIF TG_TABLE_NAME='receiving_reinstatement' THEN hid:=NEW.report_id;SELECT order_id,operation_id INTO oid,fact_op FROM public.receiving_orderevent WHERE id=NEW.event_id;
 ELSIF TG_TABLE_NAME='receiving_reinstatementline' THEN iid:=NEW.item_id;SELECT e.order_id INTO oid FROM public.receiving_reinstatement h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE h.id=NEW.reinstatement_id;
 ELSIF TG_TABLE_NAME='receiving_receiptcorrection' THEN rid:=NEW.receipt_id;
 ELSIF TG_TABLE_NAME='receiving_receiptcorrectionline' THEN SELECT receipt_id INTO rid FROM public.receiving_receiptcorrection WHERE id=NEW.correction_id;
 ELSE oid:=NEW.order_id;fact_op:=NEW.operation_id;
 END IF;
 IF rid IS NOT NULL THEN
  PERFORM public.reserve_m8_assert_receipt(rid);
  SELECT e.order_id INTO oid FROM public.receiving_receipt h JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE h.id=rid;
  FOR iid IN SELECT id FROM public.receiving_quarantineitem WHERE receipt_line_id IN (SELECT id FROM public.receiving_receiptline WHERE receipt_id=rid) UNION SELECT item_id FROM public.receiving_sourceconsumption WHERE line_id IN (SELECT id FROM public.receiving_receiptline WHERE receipt_id=rid) LOOP PERFORM public.reserve_m8_assert_item(iid); END LOOP;
 END IF;
 IF hid IS NOT NULL THEN PERFORM public.reserve_m8_assert_report(hid); END IF;
 IF iid IS NOT NULL THEN PERFORM public.reserve_m8_assert_item(iid); END IF;
 IF oid IS NOT NULL THEN PERFORM public.reserve_m7_assert_order(oid); END IF;
 IF TG_TABLE_NAME='receiving_manualreminder' THEN
  IF NOT public.reserve_m8_operation(NEW.operation_id,'orders.remind',NEW.order_id,'REPLENISHER') OR NOT EXISTS(SELECT 1 FROM public.notifications_businessevent WHERE operation_id=NEW.operation_id AND event_type='purchase.manual_reminder' AND aggregate_id=NEW.order_id AND payload->>'reminder_id'=NEW.id::text) THEN RAISE EXCEPTION 'Reminder requires its own notification event' USING ERRCODE='23514'; END IF;
 END IF;
 IF fact_op IS NOT NULL AND NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.audit_auditevent a ON a.operation_id=o.id WHERE o.id=fact_op AND i.state='CONFIRMED') THEN RAISE EXCEPTION 'Receiving facts require confirmed audit' USING ERRCODE='23514'; END IF;
 RETURN NULL;
END; $$;
"""

TABLES = [
    "orderevent",
    "receipt",
    "receiptline",
    "arrivalreport",
    "reportline",
    "unknownlink",
    "unknownlinkline",
    "quarantineitem",
    "quarantinedecision",
    "sourceconsumption",
    "reinstatement",
    "reinstatementline",
    "receiptcorrection",
    "receiptcorrectionline",
    "manualreminder",
]
for name in TABLES:
    SQL += f"CREATE TRIGGER m8_fact_insert BEFORE INSERT ON public.receiving_{name} FOR EACH ROW EXECUTE FUNCTION public.reserve_m8_fact_guard();\n"
    if name == "receipt":
        SQL += "CREATE TRIGGER m8_receipt_immutable BEFORE UPDATE OR DELETE ON public.receiving_receipt FOR EACH ROW EXECUTE FUNCTION public.reserve_m8_receipt_guard();\n"
    else:
        SQL += f"CREATE TRIGGER m8_immutable BEFORE UPDATE OR DELETE ON public.receiving_{name} FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();\n"
    SQL += f"CREATE CONSTRAINT TRIGGER m8_consistency AFTER INSERT OR UPDATE ON public.receiving_{name} DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_m8_check();\n"


class Migration(migrations.Migration):
    dependencies = [("receiving", "0002_state_functions")]
    operations = [migrations.RunSQL(SQL)]
