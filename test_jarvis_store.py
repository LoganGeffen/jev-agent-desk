import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jarvis_controller import JarvisController
from jarvis_state import JarvisState
from jarvis_store import JarvisStore
from test_jarvis_state import DOG


CHILD = '''
import os,sys
from pathlib import Path
from jarvis_controller import JarvisController
from jarvis_state import JarvisState
from jarvis_store import JarvisStore
path,phase=sys.argv[1:]
store=JarvisStore(Path(path)/'state.sqlite3')
state=JarvisState(store=store)
def effect(name,args,turn):
 if name=='set_subject': return {'ok':True,'status':'completed','subject':{'tab':'@dog','pane':'%dog','identity':'original','label':'Dog'}}
 if phase=='before_effect': os._exit(0)
 with (Path(path)/'external.input').open('ab') as stream: stream.write(args['text'].encode())
 if phase=='after_effect': os._exit(0)
 return {'ok':True,'status':'submitted'}
def provider(context,specs,execute,**kwargs):
 execute('set_subject',{'target':{'tab':'@dog','pane':'%dog','identity':'original','label':'Dog'}})
 draft=execute('prepare_literal',{'start':0,'end':len(context['current_turn']['user_text'])})
 execute('send_draft',{'draft_id':draft['draft_id']})
 return {'text':'Input submitted; task not complete.'}
JarvisController(provider,effect,state=state,authorize=lambda *a:True).submit('Café 🐕. exact','original')
store.close()
'''


class StoreTests(unittest.TestCase):
    def store(self, root):
        store = JarvisStore(Path(root) / 'state.sqlite3')
        self.addCleanup(store.close)
        return store

    def test_real_process_exit_at_effect_boundaries_never_replays_original_request(self):
        for phase in ('before_effect', 'after_effect', 'submitted'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                subprocess.run([sys.executable, '-c', CHILD, root, phase], check=True, timeout=10)
                store = self.store(root)
                restored = JarvisState(store=store)
                effects = []
                core = JarvisController(lambda *a, **k: effects.append('provider') or {'text': 'Wrong'}, lambda *a: effects.append('effect'), state=restored)
                receipt = core.submit('Café 🐕. exact', 'original')
                self.assertEqual(effects, [])
                self.assertEqual(receipt['actions'][0]['status'], 'submitted' if phase == 'submitted' else 'uncertain')
                external = Path(root) / 'external.input'
                self.assertEqual(external.read_text() if external.exists() else '', '' if phase == 'before_effect' else 'Café 🐕. exact')
                with self.assertRaises(ValueError):
                    core.submit('Changed source', 'original')
                self.assertEqual(receipt['actions'][0]['payload'], 'Café 🐕. exact')

    def test_checkpoint_failure_before_action_return_prevents_external_call(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            state = JarvisState(store=store)
            calls = []
            checkpoint = store.checkpoint
            failed = False
            def fail_action(snapshot):
                nonlocal failed
                if not failed and snapshot['actions']:
                    failed = True
                    raise RuntimeError('Synthetic disk failure before write-ahead commit')
                checkpoint(snapshot)
            store.checkpoint = fail_action
            def provider(context, specs, execute, **kwargs):
                execute('set_subject', {'target': DOG})
                draft = execute('prepare_literal', {'start': 0, 'end': 5})
                execute('send_draft', {'draft_id': draft['draft_id']})
                return {'text': 'Sent'}
            def handler(name, args, turn):
                if name == 'set_subject':
                    return {'ok': True, 'status': 'completed', 'subject': DOG}
                calls.append(args)
                return {'ok': True, 'status': 'submitted'}
            core = JarvisController(provider, handler, state=state, authorize=lambda *a: True)
            receipt = core.submit('Hello dog', 'first')
            self.assertEqual(calls, [])
            self.assertEqual(receipt['status'], 'failed')
            self.assertIn('write-ahead', receipt['error'])

    def test_background_attention_notice_and_partial_playback_restore_without_invented_heard(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            state = JarvisState(store=store)
            turn, _ = state.begin_turn('Read Dog', 'read')
            state.set_subject(turn['turn_id'], DOG)
            state.record_job(turn['turn_id'], {'ok': True, 'status': 'running', 'job_id': 'observer'})
            state.record_notice({'id': 'quiet', 'quiet': True, 'text': 'Original notice'})
            state.consume_notice('quiet')
            state.set_attention([{'heard': False, 'original': 'Exact original'}])
            state.finish_turn(turn['turn_id'], {'text': 'Café 🐕. More.'})
            playback = state.create_playback(turn['turn_id'], 'phone', 1, 'Café 🐕. More.', source_ref={'turn_id': turn['turn_id'], 'original': 'Exact original', 'style': 'original'})
            prefix = 'Café 🐕.'
            state.update_playback(playback['playback_id'], 'phone', 1, 'playing', heard_prefix={'chars': len(prefix), 'sha256': hashlib.sha256(prefix.encode()).hexdigest(), 'alignment': 'provider'})
            restored = JarvisState(store=store)
            snapshot = restored.snapshot()
            self.assertEqual(snapshot['subject'], DOG)
            self.assertEqual(snapshot['jobs']['observer']['status'], 'interrupted')
            self.assertTrue(snapshot['notices']['quiet']['consumed'])
            self.assertFalse(snapshot['attention'][0]['heard'])
            self.assertEqual(snapshot['playbacks'][playback['playback_id']]['status'], 'cancelled')
            following, _ = restored.begin_turn('What did I hear?', 'following')
            played = restored.context(following['turn_id'])['playbacks'][0]
            self.assertEqual(played['played_excerpt'], prefix)
            self.assertTrue(played['unheard_remainder'])
            with self.assertRaises(ValueError):
                restored.update_playback(playback['playback_id'], 'phone', 1, 'completed', [playback['full_boundary_id']])

    def test_failed_attention_checkpoint_does_not_later_persist_a_heard_flag(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.store(root)
            state = JarvisState(store=store)
            state.set_attention([{'heard': False}])
            checkpoint = store.checkpoint
            store.checkpoint = lambda snapshot: (_ for _ in ()).throw(RuntimeError('Synthetic unavailable disk'))
            with self.assertRaises(RuntimeError):
                state.set_attention([{'heard': True}])
            diagnostic = state.record_persistence_error('job_completion', 'job', 'Synthetic unavailable disk')
            self.assertFalse(diagnostic['persisted'])
            store.checkpoint = checkpoint
            state.changed()
            restored = JarvisState(store=store)
            self.assertFalse(restored.attention[0]['heard'])
            self.assertIn('job_completion:job', restored.persistence_errors)


if __name__ == '__main__':
    unittest.main()
