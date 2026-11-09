from django.db import migrations

FORWARD = """
CREATE FUNCTION public.reserve_case_insert_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE op uuid; b public.inventory_stockbalance%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='cases_stockcase' THEN op:=NEW.operation_id;
 ELSE SELECT operation_id INTO op FROM public.cases_stockcase WHERE id=NEW.case_id; END IF;
 IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id
   WHERE o.id=op AND o.command_type='cases.stock_conflict' AND i.state='READY') THEN
  RAISE EXCEPTION 'Case facts require a new incident confirmation' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='cases_stockcaseproduct' THEN
  SELECT * INTO b FROM public.inventory_stockbalance WHERE product_id=NEW.product_id;
  IF b.physical IS DISTINCT FROM NEW.physical OR b.reserved IS DISTINCT FROM NEW.reserved OR b.version<>NEW.stock_version THEN
   RAISE EXCEPTION 'Conflict snapshot must match unchanged stock' USING ERRCODE='23514';
  END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER case_insert_guard BEFORE INSERT ON public.cases_stockcase
 FOR EACH ROW EXECUTE FUNCTION public.reserve_case_insert_guard();
CREATE TRIGGER case_product_insert_guard BEFORE INSERT ON public.cases_stockcaseproduct
 FOR EACH ROW EXECUTE FUNCTION public.reserve_case_insert_guard();
CREATE TRIGGER stock_hold_insert_guard BEFORE INSERT ON public.cases_stockhold
 FOR EACH ROW EXECUTE FUNCTION public.reserve_case_insert_guard();

CREATE FUNCTION public.reserve_case_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE c public.cases_stockcase%ROWTYPE; cid uuid;
BEGIN
 IF TG_TABLE_NAME='cases_stockcase' THEN cid:=NEW.id; ELSE cid:=NEW.case_id; END IF;
 SELECT * INTO c FROM public.cases_stockcase WHERE id=cid;
 IF NOT EXISTS(SELECT 1 FROM public.core_operationintent i WHERE i.id=c.failed_intent_id AND i.state='REJECTED'
      AND i.command_type='inventory.adjust' AND i.actor_id=c.reporter_id)
 OR NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id
   WHERE o.id=c.operation_id AND o.actor_id=c.reporter_id AND o.command_type='cases.stock_conflict' AND i.state='CONFIRMED')
 OR NOT EXISTS(SELECT 1 FROM public.cases_stockcaseproduct WHERE case_id=cid)
 OR EXISTS(SELECT 1 FROM public.cases_stockcaseproduct cp WHERE cp.case_id=cid AND NOT EXISTS(
    SELECT 1 FROM public.cases_stockhold h WHERE h.product_id=cp.product_id AND h.case_id=cid)) THEN
  RAISE EXCEPTION 'Incomplete rejected stock incident or missing hold' USING ERRCODE='23514';
 END IF;
 IF TG_TABLE_NAME='cases_stockhold' AND NOT EXISTS(SELECT 1 FROM public.cases_stockcaseproduct WHERE case_id=cid AND product_id=NEW.product_id) THEN
  RAISE EXCEPTION 'Hold requires matching conflict product' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER case_consistency AFTER INSERT ON public.cases_stockcase
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_case_consistency();
CREATE CONSTRAINT TRIGGER case_product_consistency AFTER INSERT ON public.cases_stockcaseproduct
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_case_consistency();
CREATE CONSTRAINT TRIGGER hold_consistency AFTER INSERT ON public.cases_stockhold
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_case_consistency();
CREATE TRIGGER stock_case_immutable BEFORE UPDATE OR DELETE ON public.cases_stockcase
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
CREATE TRIGGER case_product_immutable BEFORE UPDATE OR DELETE ON public.cases_stockcaseproduct
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
CREATE TRIGGER hold_immutable BEFORE UPDATE OR DELETE ON public.cases_stockhold
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
"""
REVERSE = """
DROP TRIGGER hold_immutable ON public.cases_stockhold;
DROP TRIGGER case_product_immutable ON public.cases_stockcaseproduct;
DROP TRIGGER stock_case_immutable ON public.cases_stockcase;
DROP TRIGGER hold_consistency ON public.cases_stockhold;
DROP TRIGGER case_product_consistency ON public.cases_stockcaseproduct;
DROP TRIGGER case_consistency ON public.cases_stockcase;
DROP FUNCTION public.reserve_case_consistency();
DROP TRIGGER stock_hold_insert_guard ON public.cases_stockhold;
DROP TRIGGER case_product_insert_guard ON public.cases_stockcaseproduct;
DROP TRIGGER case_insert_guard ON public.cases_stockcase;
DROP FUNCTION public.reserve_case_insert_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("cases", "0001_initial"), ("inventory", "0002_integrity")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
