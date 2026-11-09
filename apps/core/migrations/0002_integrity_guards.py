from django.db import migrations

FORWARD = """
CREATE FUNCTION reserve_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Immutable confirmed fact' USING ERRCODE = '23514';
END;
$$;

CREATE TRIGGER operation_immutable BEFORE UPDATE OR DELETE ON core_operation
FOR EACH ROW EXECUTE FUNCTION reserve_immutable();
CREATE TRIGGER audit_immutable BEFORE UPDATE OR DELETE ON audit_auditevent
FOR EACH ROW EXECUTE FUNCTION reserve_immutable();
CREATE TRIGGER event_immutable BEFORE UPDATE OR DELETE ON notifications_businessevent
FOR EACH ROW EXECUTE FUNCTION reserve_immutable();
CREATE TRIGGER outbox_immutable BEFORE UPDATE OR DELETE ON notifications_outbox
FOR EACH ROW EXECUTE FUNCTION reserve_immutable();

CREATE FUNCTION reserve_intent_transition() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Intent tombstones are retained' USING ERRCODE = '23514';
    END IF;
    IF ROW(NEW.id, NEW.actor_id, NEW.command_type, NEW.resource_id, NEW.idempotency_key,
           NEW.payload_hash, NEW.generation_id, NEW.created_at, NEW.expires_at)
       IS DISTINCT FROM
       ROW(OLD.id, OLD.actor_id, OLD.command_type, OLD.resource_id, OLD.idempotency_key,
           OLD.payload_hash, OLD.generation_id, OLD.created_at, OLD.expires_at)
       OR OLD.state <> 'READY' OR NEW.state NOT IN ('CONFIRMED', 'REJECTED', 'EXPIRED') THEN
        RAISE EXCEPTION 'Invalid intent transition' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER intent_transition BEFORE UPDATE OR DELETE ON core_operationintent
FOR EACH ROW EXECUTE FUNCTION reserve_intent_transition();

CREATE FUNCTION reserve_assert_intent() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    iid uuid;
    current_intent core_operationintent%ROWTYPE;
    confirmed core_operation%ROWTYPE;
    operation_count integer;
BEGIN
    IF TG_TABLE_NAME = 'core_operationintent' THEN
        iid := NEW.id;
    ELSIF TG_TABLE_NAME = 'core_operation' THEN
        iid := NEW.intent_id;
    ELSIF TG_TABLE_NAME IN ('audit_auditevent', 'notifications_businessevent') THEN
        SELECT intent_id INTO iid FROM core_operation WHERE id = NEW.operation_id;
    ELSE
        SELECT o.intent_id INTO iid FROM core_operation o
        JOIN notifications_businessevent e ON e.operation_id = o.id WHERE e.id = NEW.event_id;
    END IF;
    SELECT * INTO current_intent FROM core_operationintent WHERE id = iid;
    SELECT count(*) INTO operation_count FROM core_operation WHERE intent_id = iid;
    IF current_intent.state = 'CONFIRMED' THEN
        IF operation_count <> 1 THEN
            RAISE EXCEPTION 'Confirmed intent requires exactly one operation' USING ERRCODE='23514';
        END IF;
        SELECT * INTO confirmed FROM core_operation WHERE intent_id = iid;
        IF confirmed.actor_id IS DISTINCT FROM current_intent.actor_id
           OR confirmed.command_type IS DISTINCT FROM current_intent.command_type
           OR confirmed.resource_id IS DISTINCT FROM current_intent.resource_id
           OR current_intent.response->>'operation_id' IS DISTINCT FROM confirmed.id::text
           OR current_intent.response->>'intent_id' IS DISTINCT FROM iid::text
           OR current_intent.response->>'result' IS DISTINCT FROM 'CONFIRMED'
           OR (SELECT count(*) FROM audit_auditevent WHERE operation_id=confirmed.id) <> 1
           OR NOT EXISTS (SELECT 1 FROM notifications_businessevent WHERE operation_id=confirmed.id)
           OR EXISTS (
                SELECT 1 FROM notifications_businessevent e
                WHERE e.operation_id=confirmed.id AND NOT EXISTS (
                    SELECT 1 FROM notifications_outbox b WHERE b.event_id=e.id
                )
           ) THEN
            RAISE EXCEPTION 'Incomplete or inconsistent operation audit/outbox' USING ERRCODE='23514';
        END IF;
    ELSIF operation_count <> 0 THEN
        RAISE EXCEPTION 'Unconfirmed intent cannot own an operation' USING ERRCODE='23514';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER intent_consistency AFTER INSERT OR UPDATE ON core_operationintent
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reserve_assert_intent();
CREATE CONSTRAINT TRIGGER operation_consistency AFTER INSERT ON core_operation
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reserve_assert_intent();
CREATE CONSTRAINT TRIGGER audit_consistency AFTER INSERT ON audit_auditevent
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reserve_assert_intent();
CREATE CONSTRAINT TRIGGER event_consistency AFTER INSERT ON notifications_businessevent
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reserve_assert_intent();
CREATE CONSTRAINT TRIGGER outbox_consistency AFTER INSERT ON notifications_outbox
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION reserve_assert_intent();
"""

REVERSE = """
DROP TRIGGER outbox_consistency ON notifications_outbox;
DROP TRIGGER event_consistency ON notifications_businessevent;
DROP TRIGGER audit_consistency ON audit_auditevent;
DROP TRIGGER operation_consistency ON core_operation;
DROP TRIGGER intent_consistency ON core_operationintent;
DROP TRIGGER intent_transition ON core_operationintent;
DROP TRIGGER outbox_immutable ON notifications_outbox;
DROP TRIGGER event_immutable ON notifications_businessevent;
DROP TRIGGER audit_immutable ON audit_auditevent;
DROP TRIGGER operation_immutable ON core_operation;
DROP FUNCTION reserve_assert_intent();
DROP FUNCTION reserve_intent_transition();
DROP FUNCTION reserve_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
        ("audit", "0001_initial"),
        ("notifications", "0001_initial"),
    ]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
