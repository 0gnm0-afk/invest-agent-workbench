"""Import user-entered Windows user variables without printing their values."""
import json
from pathlib import Path
import winreg
from telegram_news_credentials import ApiCredentials

ROOT=Path(__file__).resolve().parents[1]


def import_credentials(root=ROOT):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as key:
            api_id=winreg.QueryValueEx(key,'TELEGRAM_API_ID')[0]
            api_hash=winreg.QueryValueEx(key,'TELEGRAM_API_HASH')[0]
    except FileNotFoundError:
        return {'state':'waiting_for_user_environment','required_names':['TELEGRAM_API_ID','TELEGRAM_API_HASH']}
    try:
        if not isinstance(api_id,str) or not api_id.strip().isdigit() or not isinstance(api_hash,str):
            raise ValueError('invalid_api_credentials')
        store=ApiCredentials(root)
        store.save(int(api_id.strip()),api_hash.strip())
        return {'state':'api_credentials_saved','protection':'windows_current_user_dpapi',
                'user_environment_retained':True,'login_required':True}
    except Exception:
        return {'state':'import_failed','error':'check_user_api_values_or_dpapi_storage'}


if __name__=='__main__':
    print(json.dumps(import_credentials()))
