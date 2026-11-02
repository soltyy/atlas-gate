"""Bound Router proof; never an instruction to replay a prompt or file command."""
from .errors import GateError


def validate(proof, local_id, sdk_id):
    if proof is None:
        return  # Legacy/protected sessions do not get an invented empty proof.
    if (not isinstance(proof, dict) or type(proof.get('version')) is not int or proof['version'] != 1
            or proof.get('sessionId') != local_id or not sdk_id or proof.get('logicalSdkSessionId') != sdk_id
            or proof.get('state') not in ('empty_confirmed', 'protected')
            or type(proof.get('generation')) is not int or proof['generation'] < 0
            or proof.get('actionsReplayed') is not False):
        raise GateError(409, 'input_recovery_mismatch', 'Router не подтвердил прежнюю привязку подготовки файлов')


def public(proof, binding):
    return {**{k: proof[k] for k in ('version','state','generation','actionsReplayed')},
            'sessionId': binding['id'], 'logicalSdkSessionId': binding['sdk_alias']}
