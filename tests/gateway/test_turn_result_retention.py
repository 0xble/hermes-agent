"""Receipt retries preserve transcript boundaries, successor ownership and media identity."""



from pathlib import Path



from types import SimpleNamespace



import pytest



from gateway.session_authority import LiveSession, admission_row



from gateway.session_contract import SessionActor, SessionRef, Submission



from gateway.session_ingress_media import native_inputs_root



from gateway.session_persistence import authority_persistence
from gateway.session_results import admission_result, finish_result



from persistence.core.errors import AuthorityRefusal
from session_retention import after_sessions_removed
<<<<<<< HEAD
from hermes_state_runtime import RuntimeStoreError, admit_session_input, claim_session_input, get_session_admission, recover_session_inputs
||||||| dff103e05878
from hermes_state_runtime import (RuntimeStoreError, admit_session_input, begin_runtime_epoch,
    claim_session_input, get_session_admission, recover_session_inputs)
=======
from tests.fixtures.storage_seed import seed_session
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
from tests.fakes.runtime_owner import begin_runtime_epoch



def test_results_retain_only_turn_output_and_retirement_keeps_accounting(owner):
    seed_session(owner.persistence, 's', 'api_server')
    messages = []
    admissions = []
    for index in range(3):
        owner.persistence.admissions.admit(epoch=owner.epoch, principal_id='api', session_id='s',
                                           request_id=str(index), payload={'text': str(index)})
        row = admission_row(owner.persistence.admissions.claim(epoch=owner.epoch, session_id='s'))
        messages += [{'role': 'user', 'content': 'history-sentinel'},
                     {'role': 'assistant', 'content': f'answer-{index}'}]
        finish_result(authority_persistence(owner), epoch=owner.epoch, row=row, response=f'answer-{index}', outcome='completed',
            result={'result': {'messages': list(messages), 'final_response': f'answer-{index}', 'completed': True},
                    'usage': {'input_tokens': 11, 'cost': .5}})
        saved = admission_result(authority_persistence(owner), row['admission_id'])
        assert saved['result']['messages'] == [messages[-1]]
        admissions.append(row['admission_id'])
    removed = owner.persistence.sessions.delete('s', media_root=str(native_inputs_root()))
    after_sessions_removed(owner.persistence, owner.persistence.scope.profile_dir, removed, reason='delete', primary='s')
    for aid in admissions:
        saved = admission_result(authority_persistence(owner), aid)
        assert saved['result']['messages'] == []
        assert saved['usage'] == {'input_tokens': 11, 'cost': .5}
        assert saved['result']['completed'] is True
