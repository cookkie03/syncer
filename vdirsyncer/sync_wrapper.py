import sys
import logging
import re
from vdirsyncer.cli import app
from vdirsyncer.sync import Action, Upload, Update, Delete

changed_names = []

def extract_summary(item_raw):
    # Regex to find SUMMARY: or SUMMARY;LANGUAGE=en: etc
    m = re.search(r'^SUMMARY(?:;[^:]*)?:(.*)$', item_raw, re.MULTILINE | re.IGNORECASE)
    if m:
        # Vdirsyncer raw string can have escaped chars like \, or \n
        s = m.group(1).strip()
        s = s.replace(r'\,', ',').replace(r'\n', ' ').replace(r'\\', '\\')
        return s
    return "(senza titolo)"

# Patch Upload
orig_upload = Upload._run_impl
async def my_upload(self, a, b):
    try:
        if self.dest.storage.instance_name == "google_calendars":
            summary = extract_summary(self.item.raw)
            changed_names.append(f"📥 Creato (Google): {summary}")
        elif self.dest.storage.instance_name == "caldav_calendars":
            summary = extract_summary(self.item.raw)
            changed_names.append(f"📤 Creato (CalDAV): {summary}")
    except Exception as e:
        changed_names.append(f"📥 Creato: ID {self.ident} (errore nome: {e})")
    return await orig_upload(self, a, b)
Upload._run_impl = my_upload

# Patch Update
orig_update = Update._run_impl
async def my_update(self, a, b):
    try:
        if self.dest.storage.instance_name == "google_calendars":
            summary = extract_summary(self.item.raw)
            changed_names.append(f"✏️ Aggiornato (Google): {summary}")
        elif self.dest.storage.instance_name == "caldav_calendars":
            summary = extract_summary(self.item.raw)
            changed_names.append(f"✏️ Aggiornato (CalDAV): {summary}")
    except Exception as e:
        pass
    return await orig_update(self, a, b)
Update._run_impl = my_update

# Patch Delete
orig_delete = Delete._run_impl
async def my_delete(self, a, b):
    try:
        if self.dest.storage.instance_name == "google_calendars":
            changed_names.append(f"🗑 Eliminato (Google): ID {self.ident}")
        elif self.dest.storage.instance_name == "caldav_calendars":
            changed_names.append(f"🗑 Eliminato (CalDAV): ID {self.ident}")
    except Exception as e:
        pass
    return await orig_delete(self, a, b)
Delete._run_impl = my_delete

if __name__ == '__main__':
    try:
        sys.exit(app())
    finally:
        if changed_names:
            with open("/tmp/vdirsyncer_changed_names.txt", "w", encoding="utf-8") as f:
                f.write("\n".join(changed_names) + "\n")
