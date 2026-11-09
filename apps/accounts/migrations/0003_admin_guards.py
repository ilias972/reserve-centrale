from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_loginthrottle_user_temporary_expires_at_accountguard_and_more"),
        ("core", "0004_remove_operationintent_intent_state_result_and_more"),
    ]
    operations = [
        migrations.RunSQL(
            """
        INSERT INTO public.accounts_accountguard(id, initialized) VALUES (1, false);
        CREATE FUNCTION public.reserve_guard_admin_flag() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
          IF OLD.initialized AND NOT NEW.initialized OR OLD.id <> NEW.id THEN
            RAISE EXCEPTION 'Administrator initialization is irreversible' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END; $$;
        CREATE TRIGGER protect_admin_initialization BEFORE UPDATE ON public.accounts_accountguard
          FOR EACH ROW EXECUTE FUNCTION public.reserve_guard_admin_flag();
        CREATE FUNCTION public.reserve_require_admin() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$
        BEGIN
          IF EXISTS (SELECT 1 FROM public.accounts_accountguard WHERE initialized)
            AND (SELECT count(*) FROM public.accounts_user WHERE role='ADMIN' AND is_active) <> 1 THEN
            RAISE EXCEPTION 'Exactly one active administrator is required' USING ERRCODE='23514';
          END IF;
          RETURN NULL;
        END; $$;
        CREATE CONSTRAINT TRIGGER require_admin_after_user_change AFTER INSERT OR UPDATE OR DELETE
          ON public.accounts_user DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION public.reserve_require_admin();
        CREATE CONSTRAINT TRIGGER require_admin_after_initialization AFTER INSERT OR UPDATE
          ON public.accounts_accountguard DEFERRABLE INITIALLY DEFERRED
          FOR EACH ROW EXECUTE FUNCTION public.reserve_require_admin();
        """,
            """
        DROP TRIGGER require_admin_after_initialization ON public.accounts_accountguard;
        DROP TRIGGER require_admin_after_user_change ON public.accounts_user;
        DROP FUNCTION public.reserve_require_admin();
        DROP TRIGGER protect_admin_initialization ON public.accounts_accountguard;
        DROP FUNCTION public.reserve_guard_admin_flag();
        """,
        ),
    ]
