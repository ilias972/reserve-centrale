from django.db import migrations

SQL = r"""
CREATE FUNCTION reserve_notification_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Notification history retained' USING ERRCODE='23514'; END IF;
 IF TG_OP='UPDATE' THEN
   IF TG_TABLE_NAME='notifications_notification' THEN
     IF (to_jsonb(NEW)-'read_at') IS DISTINCT FROM (to_jsonb(OLD)-'read_at') OR OLD.read_at IS NOT NULL OR NEW.read_at IS NULL THEN
       RAISE EXCEPTION 'Only unread to read permitted' USING ERRCODE='23514';
     END IF;
   ELSIF TG_TABLE_NAME='notifications_pushsubscription' THEN
     IF (to_jsonb(NEW)-'revoked_at') IS DISTINCT FROM (to_jsonb(OLD)-'revoked_at') OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL THEN
       RAISE EXCEPTION 'Only revocation permitted' USING ERRCODE='23514';
     END IF;
   ELSIF TG_TABLE_NAME='notifications_workitem' THEN
     IF ROW(NEW.id,NEW.kind,NEW.outbox_id,NEW.notification_id,NEW.subscription_id,NEW.destination,NEW.mode,NEW.email_to,NEW.dedupe_key,NEW.created_at)
       IS DISTINCT FROM ROW(OLD.id,OLD.kind,OLD.outbox_id,OLD.notification_id,OLD.subscription_id,OLD.destination,OLD.mode,OLD.email_to,OLD.dedupe_key,OLD.created_at)
       OR OLD.state IN ('DONE','DEAD','SKIPPED') THEN
       RAISE EXCEPTION 'Work identity or final result is immutable' USING ERRCODE='23514';
     END IF;
   END IF;
 END IF;
 IF TG_TABLE_NAME='notifications_notification' THEN
   IF NOT EXISTS(SELECT 1 FROM notifications_businessevent e WHERE e.id=NEW.event_id AND e.operation_id=NEW.operation_id) THEN
     RAISE EXCEPTION 'Notification event mismatch' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='notifications_pushsubscription' THEN
   IF NOT EXISTS(SELECT 1 FROM accounts_usersession s WHERE s.id=NEW.session_id AND s.user_id=NEW.user_id) THEN
     RAISE EXCEPTION 'Subscription owner mismatch' USING ERRCODE='23514';
   END IF;
 ELSIF TG_TABLE_NAME='notifications_workitem' AND NEW.kind='PUSH' THEN
   IF NOT EXISTS(SELECT 1 FROM notifications_notification n JOIN notifications_pushsubscription s ON s.id=NEW.subscription_id JOIN notifications_outbox o ON o.id=NEW.outbox_id WHERE n.id=NEW.notification_id AND n.recipient_id=s.user_id AND n.event_id=o.event_id) THEN
     RAISE EXCEPTION 'Push target mismatch' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END; $$;
CREATE TRIGGER notification_guard BEFORE INSERT OR UPDATE OR DELETE ON notifications_notification FOR EACH ROW EXECUTE FUNCTION reserve_notification_guard();
CREATE TRIGGER push_guard BEFORE INSERT OR UPDATE OR DELETE ON notifications_pushsubscription FOR EACH ROW EXECUTE FUNCTION reserve_notification_guard();
CREATE TRIGGER work_guard BEFORE INSERT OR UPDATE OR DELETE ON notifications_workitem FOR EACH ROW EXECUTE FUNCTION reserve_notification_guard();
CREATE TRIGGER notification_attempt_immutable BEFORE UPDATE OR DELETE ON notifications_attempt FOR EACH ROW EXECUTE FUNCTION reserve_immutable();
CREATE TRIGGER notification_result_immutable BEFORE UPDATE OR DELETE ON notifications_attemptresult FOR EACH ROW EXECUTE FUNCTION reserve_immutable();
CREATE TRIGGER captured_email_immutable BEFORE UPDATE OR DELETE ON notifications_capturedemail FOR EACH ROW EXECUTE FUNCTION reserve_immutable();
CREATE TRIGGER notification_activation_immutable BEFORE UPDATE OR DELETE ON notifications_activation FOR EACH ROW EXECUTE FUNCTION reserve_immutable();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("notifications", "0003_workitem_email_to"),
        ("core", "0002_integrity_guards"),
    ]
    operations = [migrations.RunSQL(SQL)]
