"""Lectures transactionnelles des faits ; aucune modification des historiques M0–M7."""

from django.db import migrations

SQL = r"""
CREATE SEQUENCE public.receipt_reference_seq;
CREATE SEQUENCE public.arrival_reference_seq;

CREATE FUNCTION public.reserve_m8_effective(lid uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT jsonb_build_object('conforming',coalesce(c.conforming_after,l.conforming),
 'nonconforming',coalesce(c.nonconforming_after,l.nonconforming),'regular',coalesce(c.regular_after,l.regular),
 'surplus',coalesce(c.surplus_after,l.surplus),'nonconformity',coalesce(c.nonconformity_after,l.nonconformity),'source_grant',coalesce(c.source_grant_after,s.reinstated_quantity,0))
 FROM public.receiving_receiptline l LEFT JOIN public.receiving_sourceconsumption s ON s.line_id=l.id LEFT JOIN LATERAL (
  SELECT c.* FROM public.receiving_receiptcorrectionline c JOIN public.receiving_receiptcorrection h ON h.id=c.correction_id
  WHERE c.line_id=l.id ORDER BY h.revision DESC LIMIT 1
 ) c ON true WHERE l.id=lid;
$$;

CREATE FUNCTION public.reserve_m8_line_before(lid uuid, rev bigint) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 WITH q AS (SELECT l.quantity,
 coalesce((SELECT sum(r.regular) FROM public.receiving_receiptline r JOIN public.receiving_receipt h ON h.id=r.receipt_id JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE r.order_line_id=lid AND e.revision<rev),0)
 +coalesce((SELECT sum(c.regular_after-c.regular_before) FROM public.receiving_receiptcorrectionline c JOIN public.receiving_receiptcorrection h ON h.id=c.correction_id JOIN public.receiving_orderevent e ON e.id=h.event_id JOIN public.receiving_receiptline r ON r.id=c.line_id WHERE r.order_line_id=lid AND e.revision<rev),0) AS f,
 coalesce((SELECT sum(c.quantity) FROM public.purchasing_remainderline c JOIN public.purchasing_remainderevent e ON e.id=c.event_id WHERE c.line_id=lid AND e.revision<rev),0)
 -coalesce((SELECT sum(c.quantity) FROM public.receiving_reinstatementline c JOIN public.receiving_reinstatement h ON h.id=c.reinstatement_id JOIN public.receiving_orderevent e ON e.id=h.event_id WHERE c.line_id=lid AND e.revision<rev),0) AS a
 FROM public.purchasing_orderline l WHERE l.id=lid)
 SELECT jsonb_build_object('F',f,'A',a,'W',quantity-f-a) FROM q;
$$;

CREATE FUNCTION public.reserve_m8_item_state(iid uuid) RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 WITH origin AS (
 SELECT i.*,coalesce(r.order_line_id,ol.id,ul.order_line_id) AS order_line_id,
 coalesce(rl.report_id,uk.report_id) AS report_id,
 CASE WHEN i.receipt_line_id IS NULL THEN i.quantity ELSE (public.reserve_m8_effective(i.receipt_line_id)->>CASE WHEN i.kind='SURPLUS' THEN 'surplus' ELSE 'nonconforming' END)::bigint END AS effective_quantity
 FROM public.receiving_quarantineitem i
 LEFT JOIN public.receiving_receiptline r ON r.id=i.receipt_line_id
 LEFT JOIN public.receiving_reportline rl ON rl.id=i.report_line_id
 LEFT JOIN public.purchasing_orderline ol ON ol.id=rl.order_line_id
 LEFT JOIN public.receiving_unknownlinkline ul ON ul.id=i.link_line_id
 LEFT JOIN public.receiving_unknownlink uk ON uk.report_id=ul.link_id WHERE i.id=iid
 ), amounts AS (
 SELECT x.*,
 coalesce((SELECT sum(d.quantity) FROM public.receiving_quarantinedecision d WHERE d.item_id=iid AND d.kind='ACCEPT'),0) AS accepted,
 coalesce((SELECT sum(d.quantity) FROM public.receiving_quarantinedecision d WHERE d.item_id=iid AND d.kind='EXTERNAL'),0) AS disposed,
 coalesce((SELECT sum((public.reserve_m8_effective(s.line_id)->>'conforming')::bigint+(public.reserve_m8_effective(s.line_id)->>'nonconforming')::bigint) FROM public.receiving_sourceconsumption s WHERE s.item_id=iid),0) AS consumed,
 coalesce((SELECT sum(g.quantity) FROM public.receiving_reinstatementline g WHERE g.item_id=iid),0) AS granted,
 coalesce((SELECT sum(s.reinstated_quantity) FROM public.receiving_sourceconsumption s WHERE s.item_id=iid),0)
 +coalesce((SELECT sum(c.source_grant_after-c.source_grant_before) FROM public.receiving_receiptcorrectionline c JOIN public.receiving_sourceconsumption s ON s.line_id=c.line_id WHERE s.item_id=iid),0) AS grant_consumed,
 coalesce((SELECT sum(d.grant_released) FROM public.receiving_quarantinedecision d WHERE d.item_id=iid),0) AS grant_released,
 (SELECT count(*) FROM public.receiving_quarantinedecision d WHERE d.item_id=iid)
 +(SELECT count(*) FROM public.receiving_sourceconsumption s WHERE s.item_id=iid)
 +(SELECT count(*) FROM public.receiving_receiptcorrectionline c WHERE c.line_id=x.receipt_line_id)
 +(SELECT count(*) FROM public.receiving_receiptcorrectionline c JOIN public.receiving_sourceconsumption s ON s.line_id=c.line_id WHERE s.item_id=iid)
 +(SELECT count(*) FROM public.receiving_reinstatementline g WHERE g.item_id=iid) AS version,
 coalesce((SELECT d.kind='REFUSE' FROM public.receiving_quarantinedecision d JOIN public.core_operation o ON o.id=d.operation_id WHERE d.item_id=iid ORDER BY d.version_before DESC,o.confirmed_at DESC,d.id DESC LIMIT 1),false) AS refused
 FROM origin x)
 SELECT jsonb_build_object('id',x.id,'case_id',x.case_id,'kind',x.kind,'quantity',x.effective_quantity,
 'accepted',x.accepted,'disposed',x.disposed,'consumed',x.consumed,'granted',x.granted,'grant_consumed',x.grant_consumed,'grant_remaining',x.granted-x.grant_consumed-x.grant_released,
 'remaining',x.effective_quantity-x.accepted-x.disposed-x.consumed,'version',x.version,'refused',x.refused,
 'report_id',x.report_id,'order_id',l.order_id,'order_line_id',l.id,'product_id',l.product_id,'name',l.product_name,'unit',l.unit)
 FROM amounts x JOIN public.purchasing_orderline l ON l.id=x.order_line_id;
$$;

CREATE FUNCTION public.reserve_m8_operation(oid uuid, kind text, resource uuid, role text) RETURNS boolean LANGUAGE sql STABLE SET search_path=pg_catalog AS $$
 SELECT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id JOIN public.audit_auditevent a ON a.operation_id=o.id
 WHERE o.id=oid AND o.command_type=kind AND o.resource_id IS NOT DISTINCT FROM resource AND o.actor_role=role AND i.state='CONFIRMED');
$$;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("receiving", "0001_initial"),
        ("cases", "0009_case_receipt_case_case_typed_origin_and_more"),
    ]
    operations = [migrations.RunSQL(SQL)]
