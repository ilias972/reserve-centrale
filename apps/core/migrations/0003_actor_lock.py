from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0002_integrity_guards")]
    operations = [
        migrations.RunSQL(
            """
        CREATE FUNCTION public.reserve_lock_actor(uuid) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
        BEGIN
            PERFORM id FROM public.accounts_user WHERE id=$1 FOR SHARE;
            RETURN FOUND;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.reserve_lock_actor(uuid) FROM PUBLIC;
        """,
            "DROP FUNCTION public.reserve_lock_actor(uuid);",
        )
    ]
