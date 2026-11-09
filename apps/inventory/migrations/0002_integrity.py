from django.db import migrations

FORWARD = """
DO $$ BEGIN
 IF EXISTS(SELECT 1 FROM public.catalog_product WHERE counted_at IS NOT NULL) THEN
  RAISE EXCEPTION 'Inventory metadata without a real ledger requires explicit reconciliation before M3';
 END IF;
END; $$;
INSERT INTO public.inventory_stockbalance(product_id,physical,reserved,version,latest_entry_id)
 SELECT id,NULL,0,0,NULL FROM public.catalog_product;

CREATE FUNCTION public.reserve_new_balance() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 INSERT INTO public.inventory_stockbalance(product_id,physical,reserved,version,latest_entry_id) VALUES(NEW.id,NULL,0,0,NULL);
 RETURN NEW;
END; $$;
CREATE TRIGGER new_product_balance AFTER INSERT ON public.catalog_product
 FOR EACH ROW EXECUTE FUNCTION public.reserve_new_balance();

CREATE FUNCTION public.reserve_stock_insert_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE b public.inventory_stockbalance%ROWTYPE; o public.core_operation%ROWTYPE;
BEGIN
 PERFORM id FROM public.catalog_product WHERE id=NEW.product_id FOR UPDATE;
 SELECT * INTO b FROM public.inventory_stockbalance WHERE product_id=NEW.product_id FOR UPDATE;
 SELECT * INTO o FROM public.core_operation WHERE id=NEW.operation_id;
 IF NOT EXISTS(SELECT 1 FROM public.core_operationintent WHERE id=o.intent_id AND state='READY') THEN
  RAISE EXCEPTION 'Cannot append to a terminal operation' USING ERRCODE='23514';
 END IF;
 IF NEW.version_before IS DISTINCT FROM b.version OR NEW.physical_before IS DISTINCT FROM b.physical
   OR NEW.reserved_before IS DISTINCT FROM b.reserved THEN
  RAISE EXCEPTION 'Stock entry must start from locked current balance' USING ERRCODE='23514';
 END IF;
 IF (NEW.kind='INITIAL_COUNT' AND (o.command_type<>'inventory.initial' OR o.actor_role<>'REPLENISHER'))
   OR (NEW.kind='MANUAL_ADJUSTMENT' AND (o.command_type<>'inventory.adjust' OR o.actor_role<>'REPLENISHER')) THEN
  RAISE EXCEPTION 'Invalid inventory origin or role' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM public.cases_stockhold WHERE product_id=NEW.product_id)
    AND (NEW.delta_r>0 OR NEW.delta_p<0) THEN
  RAISE EXCEPTION 'Product on hold forbids reservations and decreases' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER stock_insert_guard BEFORE INSERT ON public.inventory_stockentry
 FOR EACH ROW EXECUTE FUNCTION public.reserve_stock_insert_guard();

CREATE FUNCTION public.reserve_inventory_fact_guard() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE op uuid;
BEGIN
 IF TG_TABLE_NAME='inventory_inventorybatch' THEN op:=NEW.operation_id;
 ELSE SELECT operation_id INTO op FROM public.inventory_inventorybatch WHERE id=NEW.batch_id;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id WHERE o.id=op AND i.state='READY') THEN
  RAISE EXCEPTION 'Inventory facts require an open confirmation' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER batch_insert_guard BEFORE INSERT ON public.inventory_inventorybatch
 FOR EACH ROW EXECUTE FUNCTION public.reserve_inventory_fact_guard();
CREATE TRIGGER batch_line_insert_guard BEFORE INSERT ON public.inventory_inventoryline
 FOR EACH ROW EXECUTE FUNCTION public.reserve_inventory_fact_guard();

CREATE FUNCTION public.reserve_assert_stock(pid uuid) RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE p public.catalog_product%ROWTYPE; b public.inventory_stockbalance%ROWTYPE;
 n bigint; initial_n bigint; dp bigint; dr bigint; vmax bigint; lastid uuid;
BEGIN
 SELECT * INTO p FROM public.catalog_product WHERE id=pid;
 IF NOT FOUND THEN RETURN; END IF;
 SELECT * INTO b FROM public.inventory_stockbalance WHERE product_id=pid;
 IF NOT FOUND THEN RAISE EXCEPTION 'Missing stock projection' USING ERRCODE='23514'; END IF;
 SELECT count(*),count(*) FILTER(WHERE kind='INITIAL_COUNT'),COALESCE(sum(delta_p),0),COALESCE(sum(delta_r),0),COALESCE(max(version_after),0)
 INTO n,initial_n,dp,dr,vmax FROM public.inventory_stockentry WHERE product_id=pid;
 IF p.counted_at IS NULL THEN
  IF n<>0 OR b.physical IS NOT NULL OR b.reserved<>0 OR b.version<>0 OR b.latest_entry_id IS NOT NULL THEN
   RAISE EXCEPTION 'Uncounted product is not zero stock' USING ERRCODE='23514';
  END IF;
  RETURN;
 END IF;
 IF n=0 OR initial_n<>1 OR n<>vmax OR b.version<>n OR b.physical IS DISTINCT FROM dp OR b.reserved<>dr THEN
  RAISE EXCEPTION 'Stock projection does not match ledger sums and versions' USING ERRCODE='23514';
 END IF;
 SELECT id INTO lastid FROM public.inventory_stockentry WHERE product_id=pid ORDER BY version_after DESC LIMIT 1;
 IF b.latest_entry_id IS DISTINCT FROM lastid OR NOT EXISTS(
  SELECT 1 FROM public.inventory_stockentry WHERE product_id=pid AND kind='INITIAL_COUNT'
    AND version_after=1 AND operation_id=p.initial_inventory_operation_id AND unit=p.unit) THEN
  RAISE EXCEPTION 'Invalid first or latest stock entry' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM (
  SELECT *,lag(physical_after) OVER(ORDER BY version_after) AS previous_p,
   lag(reserved_after) OVER(ORDER BY version_after) AS previous_r
   FROM public.inventory_stockentry WHERE product_id=pid) e
   WHERE (version_after>1 AND (physical_before IS DISTINCT FROM previous_p OR reserved_before IS DISTINCT FROM previous_r))
     OR unit<>p.unit) THEN
  RAISE EXCEPTION 'Broken stock chain or changed unit' USING ERRCODE='23514';
 END IF;
 IF EXISTS(SELECT 1 FROM public.inventory_stockentry e
  JOIN public.core_operation o ON o.id=e.operation_id JOIN public.core_operationintent i ON i.id=o.intent_id
  WHERE e.product_id=pid AND (i.state<>'CONFIRMED'
   OR NOT EXISTS(SELECT 1 FROM public.audit_auditevent a WHERE a.operation_id=o.id)
   OR (e.kind IN ('INITIAL_COUNT','MANUAL_ADJUSTMENT') AND NOT EXISTS(
    SELECT 1 FROM public.inventory_inventoryline l JOIN public.inventory_inventorybatch t ON t.id=l.batch_id
    WHERE l.entry_id=e.id AND l.product_id=e.product_id AND t.operation_id=e.operation_id
     AND l.base_version=e.version_before AND l.target_physical=e.physical_after
     AND ((e.kind='INITIAL_COUNT' AND t.kind='INITIAL') OR (e.kind='MANUAL_ADJUSTMENT' AND t.kind='DAILY')))))) THEN
  RAISE EXCEPTION 'Stock entry requires confirmed audited typed origin' USING ERRCODE='23514';
 END IF;
END; $$;

CREATE FUNCTION public.reserve_stock_consistency() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
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
CREATE CONSTRAINT TRIGGER stock_balance_consistency AFTER INSERT OR UPDATE ON public.inventory_stockbalance
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_stock_consistency();
CREATE CONSTRAINT TRIGGER stock_entry_consistency AFTER INSERT ON public.inventory_stockentry
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_stock_consistency();
CREATE CONSTRAINT TRIGGER stock_product_consistency AFTER INSERT OR UPDATE ON public.catalog_product
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_stock_consistency();
CREATE CONSTRAINT TRIGGER inventory_line_consistency AFTER INSERT ON public.inventory_inventoryline
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_stock_consistency();
CREATE CONSTRAINT TRIGGER inventory_batch_consistency AFTER INSERT ON public.inventory_inventorybatch
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_stock_consistency();

CREATE TRIGGER stock_entry_immutable BEFORE UPDATE OR DELETE ON public.inventory_stockentry
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
CREATE TRIGGER inventory_batch_immutable BEFORE UPDATE OR DELETE ON public.inventory_inventorybatch
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
CREATE TRIGGER inventory_line_immutable BEFORE UPDATE OR DELETE ON public.inventory_inventoryline
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
"""
REVERSE = """
DROP TRIGGER inventory_line_immutable ON public.inventory_inventoryline;
DROP TRIGGER inventory_batch_immutable ON public.inventory_inventorybatch;
DROP TRIGGER stock_entry_immutable ON public.inventory_stockentry;
DROP TRIGGER inventory_batch_consistency ON public.inventory_inventorybatch;
DROP TRIGGER inventory_line_consistency ON public.inventory_inventoryline;
DROP TRIGGER stock_product_consistency ON public.catalog_product;
DROP TRIGGER stock_entry_consistency ON public.inventory_stockentry;
DROP TRIGGER stock_balance_consistency ON public.inventory_stockbalance;
DROP FUNCTION public.reserve_stock_consistency();
DROP FUNCTION public.reserve_assert_stock(uuid);
DROP TRIGGER batch_line_insert_guard ON public.inventory_inventoryline;
DROP TRIGGER batch_insert_guard ON public.inventory_inventorybatch;
DROP FUNCTION public.reserve_inventory_fact_guard();
DROP TRIGGER stock_insert_guard ON public.inventory_stockentry;
DROP FUNCTION public.reserve_stock_insert_guard();
DROP TRIGGER new_product_balance ON public.catalog_product;
DROP FUNCTION public.reserve_new_balance();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0001_initial"),
        ("cases", "0001_initial"),
        ("catalog", "0004_inventory_anchor_provenance"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
