#!/usr/bin/env python3
"""Opt-in real Docker test: build, startup, restart, scheduled backup, health, verify, export."""
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path
from project_env import load_project_env


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    env.update({key:value for key,value in load_project_env(root/'.env').items() if key.startswith('TODOIST_')})
    if not env.get('TODOIST_API_TOKEN'):
        raise RuntimeError('Missing Todoist token')
    compose = shutil.which('docker-compose')
    if not compose:
        raise RuntimeError('Docker Compose executable is required')
    (root/'todoist-backup/state').mkdir(parents=True,exist_ok=True)
    evidence = Path(tempfile.mkdtemp(prefix='docker-test-',dir=root/'todoist-backup/state'))
    def run(args, timeout=120):
        result = subprocess.run(args,cwd=root,env=env,capture_output=True,text=True,timeout=timeout)
        if result.returncode:
            # Keep tool diagnostics private, never echo resolved configuration or credentials.
            (evidence/'failure.log').write_text(result.stdout+'\n'+result.stderr)
            raise RuntimeError('Docker check failed; private diagnostics saved')
        return result.stdout
    config = json.loads(run([compose,'config','--format','json']))
    service = config['services']['todoist-backup']
    service['environment']['TODOIST_API_TOKEN']='${TODOIST_API_TOKEN}'
    service['environment']['TODOIST_BACKUP_SCHEDULE']='* * * * *'
    for volume in service['volumes']:
        folder = {'/state':'state','/backup':'backup','/logs':'logs','/legacy-data':'legacy'}[volume['target']]
        local = evidence/folder; local.mkdir()
        volume['source']=str(local)
    service['healthcheck'].update(interval='15s',start_period='10s',timeout='30s',retries=3)
    service['restart']='no'
    test_config = evidence/'compose.json'
    test_config.write_text(json.dumps({'services':{'todoist-backup':service}}))
    command=[compose,'--project-name','syncer-todoist-'+evidence.name,'--file',str(test_config)]
    events=[]
    def wait_for_runs(count):
        deadline=time.monotonic()+600
        while time.monotonic()<deadline:
            dbfile=evidence/'state/todoist.sqlite3'
            if dbfile.exists():
                with sqlite3.connect(dbfile) as db:
                    rows=db.execute("SELECT id,status FROM runs WHERE finished_at IS NOT NULL ORDER BY id").fetchall()
                if len(rows)>=count:
                    if any(status=='failed' for _,status in rows):
                        raise RuntimeError('Container backup failed')
                    events.append({'run_count':len(rows),'statuses':rows})
                    return
            time.sleep(2)
        raise RuntimeError('Container backup did not finish in time')
    try:
        run(command+['config','--quiet'])
        print('Building the actual Todoist Docker image...',flush=True)
        run(command+['build','todoist-backup'],timeout=900)
        run(['docker','run','--rm','--entrypoint','python','--volume',str(root/'todoist-backup')+':/tests:ro',service['image'],'-m','unittest','discover','-s','/tests','-p','test*.py'])
        run(command+['up','--detach','todoist-backup'])
        wait_for_runs(1)
        fixture = "from pathlib import Path; from storage import Store; from collector import Collector; s=Store('/state',object_root='/backup/objects'); s.queue_file('docker-fixture-a','https://example.org/a','demo'); s.queue_file('docker-fixture-b','https://example.org/b','demo'); s.db.commit(); client=type('FixtureClient',(),{'download':lambda self,url,path,**kw: Path(path).write_bytes(b'Docker attachment fixture')})(); Collector(s,client).files(); assert not s.verify(); assert s.db.execute('SELECT count(DISTINCT hash) FROM files WHERE source LIKE ?',( 'docker-fixture%',)).fetchone()[0]==1; s.close()"
        run(command+['exec','--no-TTY','todoist-backup','python','-c',fixture])
        print('Startup and attachment storage passed; testing restart...',flush=True)
        run(command+['restart','todoist-backup'])
        wait_for_runs(2)
        print('Restart passed; waiting for an actual scheduled backup...',flush=True)
        wait_for_runs(3)
        for args in (['status','--health'],['verify'],['export']):
            run(command+['exec','--no-TTY','todoist-backup','python','/app/backup.py']+args)
        container=run(command+['ps','--quiet','todoist-backup']).strip()
        deadline=time.monotonic()+60
        while True:
            health=json.loads(run(['docker','inspect',container]))[0]['State']['Health']['Status']
            if health=='healthy': break
            if time.monotonic()>deadline: raise RuntimeError('Container did not become healthy')
            time.sleep(2)
        with sqlite3.connect(evidence/'state/todoist.sqlite3') as db:
            syncs=[json.loads(row[0]).get('full_sync') for row in db.execute(
                "SELECT p.json FROM responses r JOIN payloads p ON p.hash=r.hash WHERE r.source='sync' ORDER BY r.seq")]
        if not syncs or syncs[0] is not True or False not in syncs[1:]:
            raise RuntimeError('Full and incremental Sync were not both observed')
        exports=list((evidence/'backup/exports').glob('*/COMPLETE'))
        if not exports: raise RuntimeError('Export was not published')
        report={'ok':True,'events':events,'container_health':health,
                'checks':['compose','image build','unit tests in container','startup','restart','scheduled run','attachment storage and deduplication','status health','verify','export'],
                'sync_full_flags':syncs,'export_count':len(exports),
                'snapshot_count':len(list((evidence/'backup/snapshots').iterdir()))}
        (evidence/'result.json').write_text(json.dumps(report,indent=2))
        print(json.dumps(report,indent=2))
        print('Evidence:',evidence)
    finally:
        run(command+['down','--timeout','30'])

if __name__=='__main__':
    main()
