from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION public.reserve_stock_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE pid uuid;
BEGIN
 IF TG_TABLE_NAME='catalog_product' THEN pid:=NEW.id;
 ELSIF TG_TABLE_NAME='inventory_inventorybatch' THEN
  IF NOT EXISTS(SELECT 1 FROM public.inventory_inventoryline WHERE batch_id=NEW.id) THEN
   RAISE EXCEPTION 'Confirmed inventory batch requires lines' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
 ELSE pid:=NEW.product_id; END IF;
 PERFORM public.reserve_assert_stock(pid);
 IF TG_TABLE_NAME='inventory_inventoryline' THEN
 IF NOT EXISTS(
  SELECT 1 FROM public.inventory_stockentry e JOIN public.inventory_inventorybatch t ON t.id=NEW.batch_id
   WHERE e.id=NEW.entry_id AND e.product_id=NEW.product_id AND e.operation_id=t.operation_id
    AND e.physical_after=NEW.target_physical AND e.version_before=NEW.base_version
    AND ((t.kind='INITIAL' AND e.kind='INITIAL_COUNT') OR (t.kind='DAILY' AND e.kind='MANUAL_ADJUSTMENT'))) THEN
  RAISE EXCEPTION 'Invalid inventory line origin' USING ERRCODE='23514';
 END IF;
 END IF;
 RETURN NULL;
END; $$;
CREATE OR REPLACE FUNCTION public.reserve_case_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
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
 IF TG_TABLE_NAME='cases_stockhold' THEN
 IF NOT EXISTS(SELECT 1 FROM public.cases_stockcaseproduct WHERE case_id=cid AND product_id=NEW.product_id) THEN
  RAISE EXCEPTION 'Hold requires matching conflict product' USING ERRCODE='23514';
 END IF;
 END IF;
 RETURN NULL;
END; $$;
"""
REVERSE = """
CREATE OR REPLACE FUNCTION public.reserve_stock_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE pid uuid;
BEGIN
 IF TG_TABLE_NAME='catalog_product' THEN pid:=NEW.id;
 ELSIF TG_TABLE_NAME='inventory_inventorybatch' THEN
  IF NOT EXISTS(SELECT 1 FROM public.inventory_inventoryline WHERE batch_id=NEW.id) THEN
   RAISE EXCEPTION 'Confirmed inventory batch requires lines' USING ERRCODE='23514';
  END IF;
  RETURN NULL;
 ELSE pid:=NEW.product_id; END IF;
 PERFORM public.reserve_assert_stock(pid);
 IF TG_TABLE_NAME='inventory_inventoryline' AND NOT EXISTS(
  SELECT 1 FROM public.inventory_stockentry e JOIN public.inventory_inventorybatch t ON t.id=NEW.batch_id
   WHERE e.id=NEW.entry_id AND e.product_id=NEW.product_id AND e.operation_id=t.operation_id
    AND e.physical_after=NEW.target_physical AND e.version_before=NEW.base_version
    AND ((t.kind='INITIAL' AND e.kind='INITIAL_COUNT') OR (t.kind='DAILY' AND e.kind='MANUAL_ADJUSTMENT'))) THEN
  RAISE EXCEPTION 'Invalid inventory line origin' USING ERRCODE='23514';
 END IF;
 RETURN NULL;
END; $$;
CREATE OR REPLACE FUNCTION public.reserve_case_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
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
"""


class Migration(migrations.Migration):
    dependencies = [("inventory", "0002_integrity"), ("cases", "0002_guards")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
