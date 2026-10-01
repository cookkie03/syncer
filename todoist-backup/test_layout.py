import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from layout import migrate, publish, verify_snapshot
from storage import Store
from collector import Collector

class LayoutTests(unittest.TestCase):
    def test_migration_preserves_sources_and_resumes_after_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old, state, backup = (root / name for name in ('data','state','backup'))
            old.mkdir(); (old / 'todoist.sqlite3').write_bytes(b'original')
            (old / 'objects').mkdir(); (old / 'objects/file').write_bytes(b'attachment')
            migrate(old,state,backup)
            self.assertEqual((old / 'todoist.sqlite3').read_bytes(), b'original')
            self.assertEqual((backup / 'objects/file').read_bytes(), b'attachment')
            (state / 'todoist.sqlite3').write_bytes(b'updated')
            migrate(old,state,backup)
            self.assertEqual((state / 'todoist.sqlite3').read_bytes(), b'updated')

    def test_conflict_fails_before_any_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); old=root/'data'; state=root/'state'
            old.mkdir(); state.mkdir()
            (old/'a').write_bytes(b'a'); (old/'b').write_bytes(b'b'); (state/'b').write_bytes(b'other')
            with self.assertRaises(RuntimeError): migrate(old,state,root/'backup')
            self.assertFalse((state/'a').exists())

    def test_download_temporary_file_is_on_backup_volume(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); objects=root/'backup/objects'
            store=Store(root/'state',object_root=objects)
            with store.db: store.queue_file('fixture','https://example.org/file','demo')
            def download(url,path,**kwargs):
                self.assertEqual(Path(path).parent,objects)
                Path(path).write_bytes(b'attachment')
            client=type('Client',(),{'download':staticmethod(download)})()
            Collector(store,client).files()
            self.assertEqual(store.verify(),[])
            store.close()

    def test_atomic_snapshots_no_duplicate_current_and_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); backup=root/'backup'
            store=Store(root/'state',object_root=backup/'objects')
            with store.db:
                store.queue_file('attachment','https://example.org/file','demo')
            temporary=root/'download'; temporary.write_bytes(b'attachment bytes')
            row=store.db.execute('SELECT * FROM files').fetchone()
            store.install_file(row,temporary)
            first=publish(store,backup)
            exported=json.loads((first/'account.json').read_text())
            reference=exported['files'][0]['export_relative_path']
            self.assertEqual((first/reference).read_bytes(),b'attachment bytes')
            self.assertTrue((backup/'current').is_symlink())
            self.assertFalse(Path((backup/'current').readlink()).is_absolute())
            self.assertEqual(verify_snapshot(backup),[])
            with patch('layout.os.replace',side_effect=OSError):
                with self.assertRaises(OSError): publish(store,backup)
            self.assertEqual((backup/'current').resolve(),first.resolve())
            second=publish(store,backup)
            self.assertTrue(first.exists()); self.assertNotEqual(first,second)
            (second/'tasks.csv').write_text('corrupt')
            self.assertTrue(verify_snapshot(backup)); store.close()
