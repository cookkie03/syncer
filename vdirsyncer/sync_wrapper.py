import sys
from vdirsyncer.cli import app
from vdirsyncer.sync import Upload, Update, Delete

from sync_filters import extract_summary, format_skip_warning, is_skippable_google_write_error

changed_names = []
skipped_names = []


def storage_name(action):
    return getattr(action.dest.storage, "instance_name", "")


def record_skip(action_name, action, exc):
    summary = extract_summary(getattr(action.item, "raw", ""))
    warning = format_skip_warning(action_name, action.ident, summary, str(exc))
    skipped_names.append(warning)
    print(warning, file=sys.stderr)

# Patch Upload
orig_upload = Upload._run_impl
async def my_upload(self, a, b):
    try:
        result = await orig_upload(self, a, b)
    except Exception as exc:
        if is_skippable_google_write_error(storage_name(self), str(exc)):
            record_skip("upload", self, exc)
            return None
        raise
    summary = extract_summary(self.item.raw)
    if storage_name(self) == "google_calendars":
        changed_names.append(f"📥 Creato (Google): {summary}")
    elif storage_name(self) == "caldav_calendars":
        changed_names.append(f"📤 Creato (CalDAV): {summary}")
    return result
Upload._run_impl = my_upload

# Patch Update
orig_update = Update._run_impl
async def my_update(self, a, b):
    try:
        result = await orig_update(self, a, b)
    except Exception as exc:
        if is_skippable_google_write_error(storage_name(self), str(exc)):
            record_skip("update", self, exc)
            return None
        raise
    summary = extract_summary(self.item.raw)
    if storage_name(self) == "google_calendars":
        changed_names.append(f"✏️ Aggiornato (Google): {summary}")
    elif storage_name(self) == "caldav_calendars":
        changed_names.append(f"✏️ Aggiornato (CalDAV): {summary}")
    return result
Update._run_impl = my_update

# Patch Delete
orig_delete = Delete._run_impl
async def my_delete(self, a, b):
    try:
        result = await orig_delete(self, a, b)
    except Exception as exc:
        if is_skippable_google_write_error(storage_name(self), str(exc)):
            warning = f"warning: Skipping delete for Google event {self.ident}: {' '.join(str(exc).split())}"
            skipped_names.append(warning)
            print(warning, file=sys.stderr)
            return None
        raise
    if storage_name(self) == "google_calendars":
        changed_names.append(f"🗑 Eliminato (Google): ID {self.ident}")
    elif storage_name(self) == "caldav_calendars":
        changed_names.append(f"🗑 Eliminato (CalDAV): ID {self.ident}")
    return result
Delete._run_impl = my_delete

if __name__ == '__main__':
    try:
        sys.exit(app())
    finally:
        if changed_names:
            with open("/tmp/vdirsyncer_changed_names.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(changed_names) + "\n")
        if skipped_names:
            with open("/tmp/vdirsyncer_skipped_names.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(skipped_names) + "\n")
