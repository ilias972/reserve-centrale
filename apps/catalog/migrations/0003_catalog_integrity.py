from django.db import migrations

FORWARD = """
CREATE FUNCTION public.reserve_product_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF OLD.code IS DISTINCT FROM NEW.code THEN
  RAISE EXCEPTION 'Product reference is permanent' USING ERRCODE='23514';
 END IF;
 IF OLD.counted_at IS NOT NULL AND (
    NEW.unit IS DISTINCT FROM OLD.unit OR NEW.counted_at IS DISTINCT FROM OLD.counted_at
    OR NEW.initial_inventory_operation_id IS DISTINCT FROM OLD.initial_inventory_operation_id) THEN
  RAISE EXCEPTION 'Initial inventory anchor and unit are immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER product_unit_guard BEFORE UPDATE ON public.catalog_product
 FOR EACH ROW EXECUTE FUNCTION public.reserve_product_guard();
CREATE TRIGGER product_photo_immutable BEFORE UPDATE OR DELETE ON public.catalog_productphoto
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();
CREATE TRIGGER photo_asset_immutable BEFORE UPDATE ON public.catalog_photoasset
 FOR EACH ROW EXECUTE FUNCTION public.reserve_immutable();

CREATE FUNCTION public.reserve_catalog_consistency() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE p public.catalog_product%ROWTYPE;
BEGIN
 IF TG_TABLE_NAME='catalog_product' THEN
   SELECT * INTO p FROM public.catalog_product WHERE id=NEW.id;
   IF p.current_photo_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.catalog_productphoto WHERE id=p.current_photo_id AND product_id=p.id) THEN
    RAISE EXCEPTION 'Photo does not belong to product' USING ERRCODE='23514';
   END IF;
   IF p.counted_at IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id
    WHERE o.id=p.initial_inventory_operation_id AND o.command_type='inventory.initial'
      AND o.actor_role='REPLENISHER' AND i.state='CONFIRMED') THEN
    RAISE EXCEPTION 'Confirmed initial inventory required' USING ERRCODE='23514';
   END IF;
   IF p.status='ACTIVE' AND NOT EXISTS (
    SELECT 1 FROM public.core_operation o JOIN public.core_operationintent i ON i.id=o.intent_id
    WHERE o.id=p.activation_operation_id AND o.command_type='catalog.product.activate'
      AND o.actor_role='ADMIN' AND o.resource_id=p.id AND i.state='CONFIRMED') THEN
    RAISE EXCEPTION 'Administrator activation operation required' USING ERRCODE='23514';
   END IF;
 ELSE
   IF NOT EXISTS (
    SELECT 1 FROM public.catalog_photoasset a
     JOIN public.core_operation o ON o.id=NEW.operation_id
     JOIN public.core_operationintent i ON i.id=o.intent_id
    WHERE a.id=NEW.asset_id AND a.product_id=NEW.product_id AND a.uploader_id=o.actor_id
     AND a.intent_id=o.intent_id AND o.resource_id=NEW.product_id
     AND o.command_type='catalog.product.photo' AND i.state='CONFIRMED') THEN
    RAISE EXCEPTION 'Confirmed photo association required' USING ERRCODE='23514';
   END IF;
   IF NEW.replaces_id IS NOT NULL AND NOT EXISTS (
     SELECT 1 FROM public.catalog_productphoto WHERE id=NEW.replaces_id AND product_id=NEW.product_id) THEN
    RAISE EXCEPTION 'Previous photo belongs to another product' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NULL;
END; $$;
CREATE CONSTRAINT TRIGGER product_consistency AFTER INSERT OR UPDATE ON public.catalog_product
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_catalog_consistency();
CREATE CONSTRAINT TRIGGER product_photo_consistency AFTER INSERT ON public.catalog_productphoto
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.reserve_catalog_consistency();
"""
REVERSE = """
DROP TRIGGER product_photo_consistency ON public.catalog_productphoto;
DROP TRIGGER product_consistency ON public.catalog_product;
DROP FUNCTION public.reserve_catalog_consistency();
DROP TRIGGER photo_asset_immutable ON public.catalog_photoasset;
DROP TRIGGER product_photo_immutable ON public.catalog_productphoto;
DROP TRIGGER product_unit_guard ON public.catalog_product;
DROP FUNCTION public.reserve_product_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0002_category_floorproduct_photoasset_product_and_more"),
        ("core", "0004_remove_operationintent_intent_state_result_and_more"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
