import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

spec = importlib.util.spec_from_file_location('notion_backup', Path(__file__).with_name('backup.py'))
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)
verify_spec=importlib.util.spec_from_file_location('notion_verify',Path(__file__).resolve().parent.parent/'setup/verify-notion-backup.py')
v=importlib.util.module_from_spec(verify_spec)
verify_spec.loader.exec_module(v)
P = '11111111-1111-1111-1111-111111111111'
C = '22222222-2222-2222-2222-222222222222'
D = '33333333-3333-3333-3333-333333333333'
S = '44444444-4444-4444-4444-444444444444'


def page(oid, name, parent=None):
    return {'object':'page','id':oid,'parent':parent or {'type':'workspace','workspace':True},
            'properties':{'Name':{'id':'title','type':'title','title':[{'plain_text':name}]}}}


def fixture(text='hello'):
    crawl = b.Crawl(None)
    crawl.objects={P:page(P,'Parent'),C:page(C,'Child',{'type':'page_id','page_id':P}),
                   D:{'id':D,'object':'database','title':[{'plain_text':'DB'}],'parent':{'type':'page_id','page_id':P}},
                   S:{'id':S,'object':'data_source','title':[{'plain_text':'Table'}],'parent':{'type':'database_id','database_id':D}}}
    crawl.objects[C]['parent']={'type':'data_source_id','data_source_id':S}
    crawl.markdown={P:text,C:'row'}
    crawl.blocks={P:[],C:[]}
    crawl.rows={S:[crawl.objects[C]]}
    return crawl


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)/'backup'
    def tearDown(self):
        self.temp.cleanup()
    def test_hierarchy_csv_and_portable_current(self):
        result=b.snapshot(self.root,fixture(),retention=0)
        paths={oid:Path(obj['path']) for oid,obj in result['objects'].items()}
        self.assertTrue(paths[C].is_relative_to(paths[P]))
        text=(self.root/'current'/paths[S]/'rows.csv').read_text()
        self.assertIn('_notion_page_id,_local_page,Name',text)
        self.assertIn('Child',text)
        self.assertFalse((self.root/'current').readlink().is_absolute())
        self.assertTrue(b.health(self.root,60))
    def test_versions_reuse_unchanged_and_preserve_modified_files(self):
        first=b.snapshot(self.root,fixture(),retention=0)
        old=(self.root/'current').resolve()
        b.snapshot(self.root,fixture('changed'),retention=0)
        path=Path(first['objects'][P]['path'])/'page.md'
        self.assertEqual((old/path).read_text(),'hello')
        self.assertEqual((self.root/'current'/path).read_text(),'changed')
        raw=Path('json')/C/'content.json'
        self.assertEqual((old/raw).stat().st_ino,(self.root/'current'/raw).stat().st_ino)
        self.assertEqual(len(list((self.root/'snapshots').iterdir())),2)
    def test_download_failure_keeps_current_and_cleans_staging(self):
        b.snapshot(self.root,fixture(),retention=0)
        old=(self.root/'current').readlink()
        crawl=fixture()
        crawl.objects[P]['cover']={'type':'file','file':{'url':'https://example.com/file'}}
        with self.assertRaises(RuntimeError):
            b.snapshot(self.root,crawl,downloader=Mock(side_effect=RuntimeError('fail')))
        self.assertEqual((self.root/'current').readlink(),old)
        self.assertEqual(list(self.root.glob('.staging-*')),[])
    def test_missing_objects_block_publish(self):
        b.snapshot(self.root,fixture(),retention=0)
        crawl=fixture()
        del crawl.objects[C]; del crawl.markdown[C]; del crawl.blocks[C]; crawl.rows[S]=[]
        with self.assertRaisesRegex(RuntimeError,'missing'):
            b.snapshot(self.root,crawl)
        self.assertEqual(len(list((self.root/'snapshots').iterdir())),1)
    def test_notional_assets_rewrite_markdown_offline(self):
        url='https://example.com/file.png?signature=private'
        crawl=fixture('[image]('+url+')')
        crawl.blocks[P]=[{'type':'image','image':{'type':'file','file':{'url':url}}}]
        result=b.snapshot(self.root,crawl,downloader=lambda url,limit:b'image')
        path=self.root/'current'/result['objects'][P]['path']/'page.md'
        self.assertNotIn('signature',path.read_text())
        self.assertIn('assets/',path.read_text())
        self.assertEqual(len(list((self.root/'current/assets').iterdir())),1)
    def test_expired_attachment_is_refreshed(self):
        url='https://example.com/file.png?old=1'
        fresh='https://example.com/file.png?new=1'
        crawl=fixture('[image]('+url+')')
        crawl.objects[P]['cover']={'type':'file','file':{'url':url}}
        api=Mock()
        api.request.return_value={**crawl.objects[P], 'cover':{'type':'file','file':{'url':fresh}}}
        crawl.api=api
        crawl.full_properties=Mock()
        downloader=Mock(side_effect=[RuntimeError('Attachment download HTTP 403'),b'asset'])
        result=b.snapshot(self.root,crawl,downloader=downloader)
        self.assertEqual(downloader.call_args_list[1].args[0],fresh)
        self.assertTrue(result['files'])

    def test_failed_last_run_changes_health_without_hiding_current(self):
        b.snapshot(self.root,fixture())
        self.assertTrue(b.health(self.root,60))
        b.record_run(self.root,'failed')
        self.assertFalse(b.health(self.root,60))
        self.assertTrue((self.root/'current/COMPLETE').is_file())

    def test_retention_only_when_enabled(self):
        b.snapshot(self.root,fixture(),retention=1)
        b.snapshot(self.root,fixture(),retention=1)
        self.assertEqual(len(list((self.root/'snapshots').iterdir())),1)
    def test_internal_page_and_database_links_are_local_and_encoded(self):
        crawl=fixture('https://app.notion.com/p/'+C.replace('-','')+' https://www.notion.so/'+D)
        result=b.snapshot(self.root,crawl)
        md=(self.root/'current'/result['objects'][P]['path']/'page.md').read_text()
        self.assertNotIn('https://',md)
        self.assertIn('page.md',md)
        self.assertIn('index.md',md)
        self.assertIn('%20',md)

    def test_utf8_filename_fits_linux_segment_limit(self):
        obj=page(P,'🌿'*100)
        paths=b.object_paths({P:obj})
        self.assertLessEqual(len(paths[P].name.encode()),255)

    def test_empty_database_retains_schema_columns_and_title(self):
        crawl=fixture()
        crawl.objects[S]['properties']={'Name':{'type':'title','title':{}},'Done':{'type':'checkbox','checkbox':{}}}
        crawl.rows[S]=[]
        result=b.snapshot(self.root,crawl)
        self.assertIn('Table',result['objects'][S]['path'])
        csv_path=self.root/'current'/result['objects'][S]['path']/'rows.csv'
        self.assertIn('Done,Name',csv_path.read_text())

    def test_example_url_in_markdown_is_not_an_attachment(self):
        downloader=Mock()
        crawl=fixture('Example: `https://[YOUR_HOST]/path`')
        result=b.snapshot(self.root,crawl,downloader=downloader)
        downloader.assert_not_called()
        md=self.root/'current'/result['objects'][P]['path']/'page.md'
        self.assertEqual(md.read_text(),crawl.markdown[P])

    def test_verifier_handles_original_malformed_external_link(self):
        b.snapshot(self.root,fixture('[routine]([https://hevy.com/routines])'))
        self.assertEqual(v.verify(self.root)['pages'],2)

    def test_verifier_still_rejects_missing_local_link(self):
        b.snapshot(self.root,fixture())
        current=self.root/'current'
        bad=b'[page](../missing/page.md)'
        (current/'index.md').write_bytes(bad)
        manifest=json.loads((current/'manifest.json').read_text())
        manifest['files']['index.md']={'sha256':b.hashlib.sha256(bad).hexdigest(),'bytes':len(bad)}
        (current/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError,'local reference'):
            v.verify(self.root)

    def test_legacy_export_id_links_resolve_to_current_page(self):
        legacy='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        crawl=fixture(f'[Child](Child%20{legacy}.md)')
        crawl.objects[C]['properties']['Name']['title']=[{'plain_text':f'Child {legacy}'}]
        result=b.snapshot(self.root,crawl)
        md=(self.root/'current'/result['objects'][P]['path']/'page.md').read_text()
        self.assertNotIn('.md)',md.split('](',1)[0])
        self.assertIn('page.md)',md)
        self.assertEqual(v.verify(self.root)['unresolved_source_links'],0)

    def test_original_orphan_link_is_preserved_and_reported(self):
        crawl=fixture('[Original](Missing%20aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.md)')
        result=b.snapshot(self.root,crawl)
        self.assertEqual(len(result['unresolved_source_links']),1)
        self.assertEqual(v.verify(self.root)['unresolved_source_links'],1)
        self.assertEqual((self.root/'current'/result['objects'][P]['path']/'page.md').read_text(),crawl.markdown[P])

    def test_root_relative_notion_link_becomes_external_when_not_backed_up(self):
        ref='/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?v=example'
        result=b.snapshot(self.root,fixture(f'[Other]({ref})'))
        md=(self.root/'current'/result['objects'][P]['path']/'page.md').read_text()
        self.assertIn('https://www.notion.so'+ref,md)
        self.assertEqual(v.verify(self.root)['unresolved_source_links'],0)

    @patch.object(b.requests,'get')
    def test_native_streamed_asset_is_verified_and_reused(self,get):
        response=MagicMock(ok=True)
        response.iter_content.side_effect=lambda size:iter([b'x'*65536]*4)
        get.return_value.__enter__.return_value=response
        crawl=fixture()
        crawl.objects[P]['cover']={'type':'file','file':{'url':'https://example.com/file.bin'}}
        first=b.snapshot(self.root,crawl,retention=0)
        asset=next(path for path in first['files'] if path.startswith('assets/'))
        old=(self.root/'current').resolve()
        second=b.snapshot(self.root,crawl,retention=0)
        self.assertEqual((old/asset).stat().st_ino,(self.root/'current'/asset).stat().st_ino)
        self.assertGreater(second['reused_files'],0)
        self.assertEqual(v.verify(self.root)['assets'],1)
        self.assertFalse((self.root/'current'/'.attachment.tmp').exists())

    def test_disk_spool_keeps_export_equivalent_and_file_discovery_working(self):
        original=fixture()
        disk=b.Crawl(None,spool_dir=Path(self.temp.name)/'spool')
        disk.objects=original.objects;disk.rows=original.rows
        disk.blocks.update(original.blocks);disk.markdown.update(original.markdown)
        result=b.snapshot(self.root,disk)
        self.assertEqual(v.verify(self.root)['pages'],2)
        url='https://example.com/asset'
        disk.blocks[P]=[{'type':'image','image':{'type':'file','file':{'url':url}}}]
        self.assertEqual(list(b.walk_files(disk.blocks)),[url])
        self.assertIsInstance(disk.blocks,b.DiskMap)
        self.assertEqual(disk.markdown[P],'hello')

    def test_main_uses_disk_spool_and_removes_it_after_completion(self):
        def run(crawl,roots=()):
            self.assertIsInstance(crawl.blocks,b.DiskMap)
            original=fixture()
            crawl.objects=original.objects;crawl.rows=original.rows
            crawl.blocks.update(original.blocks);crawl.markdown.update(original.markdown)
            return crawl
        with patch.dict(os.environ,{'NOTION_PAT':'not-a-secret'},clear=True), patch.object(b.API,'request'), patch.object(b.Crawl,'run',run):
            self.assertEqual(b.main(['--backup-dir',str(self.root)]),0)
        self.assertEqual(list(self.root.glob('.notion-crawl-*')),[])
        self.assertTrue(b.health(self.root,60))

    def test_unfinished_backup_not_healthy(self):
        self.root.mkdir()
        self.assertFalse(b.health(self.root,60))
    def test_cycle_rejected(self):
        obj=page(P,'Cycle',{'type':'page_id','page_id':P})
        with self.assertRaisesRegex(RuntimeError,'Cycle'):
            b.object_paths({P:obj})


class DownloadTests(unittest.TestCase):
    @patch.object(b.requests, 'get')
    def test_attachment_written_before_next_chunk_is_requested(self, get):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'attachment'
            chunk=b'x'*65536
            def chunks(size):
                self.assertLessEqual(size,65536)
                for index in range(32):
                    if index:self.assertEqual(target.stat().st_size,index*len(chunk))
                    yield chunk
            response=Mock(ok=True)
            response.iter_content.side_effect=chunks
            get.return_value=MagicMock()
            get.return_value.__enter__.return_value=response
            digest,total=b.download('https://example.com/file',4*1024*1024,target)
            self.assertEqual(total,32*len(chunk))
            self.assertEqual(digest,b.hashlib.sha256(chunk*32).hexdigest())
            self.assertEqual(target.stat().st_size,total)
            self.assertNotIn('headers',get.call_args.kwargs)

    @patch.object(b.requests, 'get')
    def test_oversized_stream_removes_partial_file(self,get):
        with tempfile.TemporaryDirectory() as directory:
            target=Path(directory)/'attachment'
            response=Mock(ok=True)
            response.iter_content.return_value=iter([b'x'*65536,b'y'*65536])
            get.return_value=MagicMock()
            get.return_value.__enter__.return_value=response
            with self.assertRaisesRegex(RuntimeError,'NOTION_MAX_FILE_BYTES'):
                b.download('https://example.com/file',70000,target)
            self.assertFalse(target.exists())


class APITests(unittest.TestCase):
    def test_search_pagination(self):
        api=b.API('not-a-secret',delay=0)
        api.request=Mock(side_effect=[{'results':[1],'has_more':True,'next_cursor':'abc'}, {'results':[2],'has_more':False}])
        self.assertEqual(list(api.pages('POST','/search')), [1,2])
        self.assertEqual(api.request.call_args_list[1].kwargs['json']['start_cursor'],'abc')
    def test_bad_cursor_fails(self):
        api=b.API('not-a-secret',delay=0)
        api.request=Mock(return_value={'results':[],'has_more':True,'next_cursor':None})
        with self.assertRaises(RuntimeError): list(api.pages('GET','/blocks/x/children'))
    @patch.object(b.time,'sleep')
    def test_retry_after_and_no_token_in_errors(self,sleep):
        api=b.API('super-secret',delay=0)
        retry=Mock(status_code=429,headers={'Retry-After':'2'})
        success=Mock(status_code=200,ok=True); success.json.return_value={'ok':True}
        api.session.request=Mock(side_effect=[retry,success])
        self.assertEqual(api.request('GET','/users/me'),{'ok':True})
        sleep.assert_any_call(2)
        api.session.request=Mock(return_value=Mock(status_code=401,ok=False))
        with self.assertRaises(RuntimeError) as caught: api.request('GET','/users/me')
        self.assertNotIn('super-secret',str(caught.exception))
    def test_full_property_pagination(self):
        api=Mock()
        api.request.side_effect=[{'object':'list','results':[{'title':{'plain_text':'One'}}],'has_more':True,'next_cursor':'abc'},
                                 {'object':'list','results':[{'title':{'plain_text':'Two'}}],'has_more':False}]
        obj=page(P,'short')
        obj['properties']['Name']['title']=[{'plain_text':'x'}]*25
        b.Crawl(api).full_properties(obj)
        self.assertEqual(b.title(obj),'OneTwo')
    def test_original_synced_block_uses_embedded_children(self):
        child={'object':'block','id':D,'type':'paragraph','has_children':False}
        original={'object':'block','id':C,'type':'synced_block','has_children':True,
                  'synced_block':{'synced_from':None,'children':[child]}}
        class Fake:
            def pages(self, method, path, **kwargs):
                if path == f'/blocks/{P}/children':
                    self.path=path
                    return [original]
                raise b.NotionHTTPError(404,path)
            def request(self, method, path, **kwargs):
                raise AssertionError(f'unexpected request {path}')
        fake=Fake()
        blocks=b.Crawl(fake).blocks_for(P,kind='page')
        self.assertEqual(fake.path,f'/blocks/{P}/children')
        self.assertEqual(blocks[0]['_children'],[child])

    def test_duplicate_synced_block_reads_original_embedded_children(self):
        child={'object':'block','id':S,'type':'paragraph','has_children':False}
        duplicate={'object':'block','id':C,'type':'synced_block','has_children':True,
                   'synced_block':{'synced_from':{'type':'block_id','block_id':D}}}
        original={'object':'block','id':D,'type':'synced_block','has_children':True,
                  'synced_block':{'synced_from':None,'children':[child]}}
        class Fake:
            def pages(self, method, path, **kwargs):
                if path == f'/blocks/{P}/children': return [duplicate]
                raise b.NotionHTTPError(404,path)
            def request(self, method, path, **kwargs):
                if path == f'/blocks/{D}': return original
                raise AssertionError(f'unexpected request {path}')
        blocks=b.Crawl(Fake()).blocks_for(P,kind='page')
        self.assertEqual(blocks[0]['_children'],[child])

    def test_block_children_404_requires_fresh_metadata(self):
        class Fake:
            def __init__(self, block, parent=None):
                self.block=block; self.parent=parent
            def pages(self, method, path, **kwargs):
                raise b.NotionHTTPError(404,path)
            def request(self, method, path, **kwargs):
                if path == f'/blocks/{C}': return self.block
                if path == f'/pages/{P}': return self.parent
                raise AssertionError(path)

        active_page=page(P,'Parent')
        trashed_block={'object':'block','id':C,'archived':True,'parent':{'type':'page_id','page_id':P}}
        crawl=b.Crawl(Fake(trashed_block,active_page))
        self.assertEqual(crawl.blocks_for(C),[])
        self.assertIn(C,crawl.excluded)

        active_block={'object':'block','id':C,'archived':False,'parent':{'type':'page_id','page_id':P}}
        crawl=b.Crawl(Fake(active_block,active_page))
        with self.assertRaisesRegex(b.NotionHTTPError,'404'):
            crawl.blocks_for(C)
        self.assertNotIn(C,crawl.excluded)

    def test_block_children_404_respects_archived_parent_from_fresh_metadata(self):
        active_block={'object':'block','id':C,'archived':False,'parent':{'type':'page_id','page_id':P}}
        archived_page={**page(P,'Parent'),'archived':True}
        class Fake:
            def pages(self, method, path, **kwargs): raise b.NotionHTTPError(404,path)
            def request(self, method, path, **kwargs):
                if path == f'/blocks/{C}': return active_block
                if path == f'/pages/{P}': return archived_page
                raise AssertionError(path)
        crawl=b.Crawl(Fake())
        self.assertEqual(crawl.blocks_for(C),[])
        self.assertIn(C,crawl.excluded)

    def test_crawl_traverses_database_and_rows_not_just_search(self):
        class Fake:
            def request(self,method,path,**kwargs):
                if path == f'/pages/{P}': return page(P,'Parent')
                if path == f'/pages/{C}': return page(C,'Child',{'type':'data_source_id','data_source_id':S})
                if '/properties/' in path: return {'object':'list','results':[{'title':{'plain_text':'A'}}],'has_more':False}
                if path.endswith('/markdown'): return {'markdown':'body','truncated':False,'unknown_block_ids':[]}
                if path == f'/databases/{D}': return {'object':'database','id':D,'data_sources':[{'id':S}]}
                if path == f'/data_sources/{S}': return {'object':'data_source','id':S,'parent':{'type':'database_id','database_id':D}}
                raise AssertionError(path)
            def pages(self,method,path,**kwargs):
                if path=='/search': return [page(P,'Parent')] if kwargs['filter']['value']=='page' else []
                if path==f'/blocks/{P}/children': return [{'id':D,'type':'child_database','has_children':True}]
                if path==f'/blocks/{C}/children': return []
                if path==f'/data_sources/{S}/query': return [page(C,'Child')]
                raise AssertionError(path)
        with tempfile.TemporaryDirectory() as directory:
            result=b.Crawl(Fake(),spool_dir=directory).run()
            self.assertEqual(set(result.objects),{P,C,D,S})
            self.assertEqual(result.markdown[P],'body')
    def test_database_rows_are_processed_before_fetching_next_row(self):
        api=Mock()
        api.request.return_value={'object':'data_source','id':S}
        processed=[]
        def rows(*args,**kwargs):
            yield page(P,'First')
            self.assertEqual(processed,[P])
            yield page(C,'Second')
        api.pages.side_effect=rows
        crawl=b.Crawl(api)
        crawl.page=lambda oid,metadata=None:processed.append(oid)
        crawl.source(S)
        self.assertEqual(processed,[P,C])
        self.assertEqual(crawl.rows[S],[{'id':P},{'id':C}])

    def test_required_root_access_checked_before_workspace_crawl(self):
        api=Mock()
        api.request.side_effect=RuntimeError('Notion HTTP 404')
        with self.assertRaises(RuntimeError): b.Crawl(api).run([('database',D)])
        api.pages.assert_not_called()

    def test_truncated_markdown_fails(self):
        api=Mock()
        api.request.side_effect=[page(P,'Parent'),
                                 {'markdown':'partial','truncated':True,'unknown_block_ids':[C]}]
        api.pages.return_value=[]
        with self.assertRaisesRegex(RuntimeError,'Incomplete Markdown'): b.Crawl(api).page(P)

    def test_unsupported_button_does_not_cancel_workspace_backup(self):
        api=Mock()
        api.pages.return_value=[{'object':'block','id':C,'type':'unsupported','has_children':False}]
        text=f'Before\n<unknown url="https://app.notion.com/p/{P.replace("-", "")}#{C.replace("-", "")}" alt="button"/>\nAfter'
        api.request.side_effect=[page(P,'Parent'),{'markdown':text,'truncated':True,'unknown_block_ids':[C]}]
        crawl=b.Crawl(api)
        crawl.page(P)
        self.assertEqual(crawl.markdown[P],text)
        self.assertEqual(crawl.blocks[P][0]['type'],'unsupported')
        self.assertTrue(crawl.warnings)

    def test_truncated_subtree_is_recovered_in_place(self):
        api=Mock()
        api.pages.return_value=[]
        api.request.side_effect=[page(P,'Parent'),
            {'markdown':f'Before\n<unknown url="https://app.notion.com/p/{P}#{C}"/>\nAfter','truncated':True,'unknown_block_ids':[C]},
            {'markdown':'Recovered subtree','truncated':False,'unknown_block_ids':[]}]
        crawl=b.Crawl(api)
        crawl.page(P)
        self.assertEqual(crawl.markdown[P],'Before\nRecovered subtree\nAfter')

    def test_recurring_unknown_subtree_fails_without_loop(self):
        api=Mock()
        api.pages.return_value=[]
        unknown={'markdown':f'<unknown url="https://app.notion.com/p/{P}#{C}"/>','truncated':True,'unknown_block_ids':[C]}
        api.request.side_effect=[page(P,'Parent'),unknown,unknown]
        with self.assertRaisesRegex(RuntimeError,'Incomplete Markdown'): b.Crawl(api).page(P)


if __name__=='__main__': unittest.main()
