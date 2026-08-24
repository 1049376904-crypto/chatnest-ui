"""密码 → token。跟上游一样是无状态 HMAC：token 就是密钥的确定性摘要。

它不带过期时间，也无法单独吐回某一个 token——想让所有已登录设备下线，
改 CHAT_SECRET 重启。自己一个人用够了，多人场景请换成带 exp 的 JWT。
"""

import hashlib
import hmac

from server.config import CHAT_PASSWORD, CHAT_SECRET


def _expected() -> str:
    return hmac.new(CHAT_SECRET.encode(), b"chat-v1", hashlib.sha256).hexdigest()


def issue_token(password: str) -> str | None:
    if hmac.compare_digest(password, CHAT_PASSWORD):
        return _expected()
    return None


def verify_token(token: str) -> bool:
    return hmac.compare_digest(token, _expected())
