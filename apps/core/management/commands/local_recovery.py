import json
import tempfile

from django.core.management.base import BaseCommand, CommandError

from apps.core import local_recovery as recovery


class Command(BaseCommand):
    help = "Sauvegarde chiffrée locale ; restauration dans une base neuve uniquement."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["keygen", "backup", "verify", "restore"])
        parser.add_argument("--key-file", required=True)
        parser.add_argument("--directory")
        parser.add_argument("--destination")
        parser.add_argument("--database")
        parser.add_argument("--trust-local-backup", action="store_true")

    def handle(self, *args, **options):
        try:
            action = options["action"]
            if action == "keygen":
                recovery.keygen(options["key_file"])
                result = {"key_created": True}
            elif not options["directory"]:
                raise recovery.RecoveryError("DIRECTORY_REQUIRED")
            elif action == "backup":
                result = recovery.backup(options["directory"], options["key_file"])
            elif action == "verify":
                with tempfile.TemporaryDirectory() as folder:
                    recovery.unpack_verified(options["directory"], options["key_file"], folder)
                result = {"integrity_verified": True, "restored_and_verified": False}
            else:
                if (
                    not options["trust_local_backup"]
                    or not options["database"]
                    or not options["destination"]
                ):
                    raise recovery.RecoveryError("EXPLICIT_TRUST_AND_NEW_TARGET_REQUIRED")
                result = recovery.restore(
                    options["directory"],
                    options["key_file"],
                    database=options["database"],
                    destination=options["destination"],
                )
        except recovery.RecoveryError as exc:
            raise CommandError(str(exc)) from None
        except Exception:
            raise CommandError(
                "RECOVERY_FAILED : aucune réussite annoncée ; consulter la procédure locale."
            ) from None
        self.stdout.write(json.dumps(result, ensure_ascii=False))
