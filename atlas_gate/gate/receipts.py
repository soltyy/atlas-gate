"""Проверка Router receipt: отсутствие/повреждение не доказывает terminal outcome."""
import httpx


def completed_response(saved, request_id):
    if (not isinstance(saved, dict) or saved.get('state') != 'complete'
            or saved.get('requestId', request_id) != request_id
            or type(saved.get('status')) is not int or not 100 <= saved['status'] <= 599
            or 'response' not in saved):
        return None
    return httpx.Response(saved['status'], json=saved['response'])
