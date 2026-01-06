import time
import hmac
import hashlib
import base64
import urllib.parse
import requests
import logging
import os
from dotenv import load_dotenv

load_dotenv()

class DingTalkNotifier:
    def __init__(self, access_token=None, secret=None):
        self.access_token = access_token or os.getenv("DING_ROBOT_ACCESS_TOKEN")
        self.secret = secret or os.getenv("DING_ROBOT_SECRET")
        
        if not self.access_token or not self.secret:
            logging.warning("DingTalk configuration missing. Notifications will not be sent.")

    def _get_signed_url(self):
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f'{timestamp}\n{self.secret}'
        hmac_code = hmac.new(self.secret.encode('utf-8'), string_to_sign.encode('utf-8'), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        return f'https://oapi.dingtalk.com/robot/send?access_token={self.access_token}&timestamp={timestamp}&sign={sign}'

    def send_text(self, msg, at_user_ids=None, at_mobiles=None, is_at_all=False):
        """发送纯文本消息"""
        if not self.access_token: return
        
        url = self._get_signed_url()
        body = {
            "at": {
                "isAtAll": is_at_all,
                "atUserIds": at_user_ids or [],
                "atMobiles": at_mobiles or []
            },
            "text": {
                "content": msg
            },
            "msgtype": "text"
        }
        return self._post(url, body)

    def send_markdown(self, title, text, at_user_ids=None, at_mobiles=None, is_at_all=False):
        """发送Markdown消息"""
        if not self.access_token: return
        
        url = self._get_signed_url()
        body = {
            "at": {
                "isAtAll": is_at_all,
                "atUserIds": at_user_ids or [],
                "atMobiles": at_mobiles or []
            },
            "markdown": {
                "title": title,
                "text": text
            },
            "msgtype": "markdown"
        }
        return self._post(url, body)

    def _post(self, url, body):
        try:
            resp = requests.post(url, json=body, headers={'Content-Type': 'application/json'}, timeout=10)
            resp.raise_for_status()
            logging.info(f"DingTalk notification sent: {resp.text}")
            return resp.json()
        except Exception as e:
            logging.error(f"Failed to send DingTalk notification: {e}")
            return None

if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.INFO)
    notifier = DingTalkNotifier()
    notifier.send_markdown("Markdown Test", "# Hello \n **Bold Text** \n > Quote")
