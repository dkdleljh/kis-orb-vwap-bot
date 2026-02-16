"""
Enterprise KIS Trading Bot - Security Module
====================================

Advanced security features including encryption, authentication,
authorization, and audit logging for the trading system.
"""

import os
import hashlib
import hmac
import json
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
import base64
import jwt
from passlib.context import CryptContext

from ..config.settings import Settings
from ..utils.logger import get_logger

logger = get_logger(__name__)


class EncryptionManager:
    """암호화 관리자"""

    def __init__(self, encryption_key: str):
        self.encryption_key = encryption_key.encode()
        self.fernet = Fernet(self._derive_key())
        self.pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

    def _derive_key(self) -> bytes:
        """PBKDF2를 사용한 키 파생"""
        salt = b"kis_trading_salt"  # 실제 운영에서는 환경별로 다른 salt 사용
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=100000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(self.encryption_key))
        return key

    def encrypt_data(self, data: str) -> str:
        """데이터 암호화"""
        try:
            encrypted_data = self.fernet.encrypt(data.encode())
            return base64.urlsafe_b64encode(encrypted_data).decode()
        except Exception as e:
            logger.error(f"Encryption failed: {e}")
            raise

    def decrypt_data(self, encrypted_data: str) -> str:
        """데이터 복호화"""
        try:
            decoded_data = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted_data = self.fernet.decrypt(decoded_data)
            return decrypted_data.decode()
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise

    def hash_password(self, password: str) -> str:
        """비밀번호 해시"""
        return self.pwd_context.hash(password)

    def verify_password(self, plain_password: str, hashed_password: str) -> bool:
        """비밀번호 검증"""
        return self.pwd_context.verify(plain_password, hashed_password)

    def generate_api_signature(
        self, method: str, url: str, params: Dict[str, Any], api_secret: str
    ) -> str:
        """API 서명 생성 (HMAC-SHA256)"""
        try:
            # 쿼리 스트링 정렬
            sorted_params = sorted(params.items())
            query_string = "&".join([f"{k}={v}" for k, v in sorted_params])

            # 서명할 문자열 생성
            sign_string = f"{method.upper()} {url}?{query_string}"

            # HMAC-SHA256 서명
            signature = hmac.new(
                api_secret.encode("utf-8"), sign_string.encode("utf-8"), hashlib.sha256
            ).hexdigest()

            return signature

        except Exception as e:
            logger.error(f"Signature generation failed: {e}")
            raise


class AuthenticationManager:
    """인증 관리자"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.encryption_manager = EncryptionManager(settings.ENCRYPTION_KEY)
        self.active_sessions: Dict[str, Dict[str, Any]] = {}

    def create_jwt_token(self, user_id: str, permissions: List[str] = None) -> str:
        """JWT 토큰 생성"""
        try:
            payload = {
                "user_id": user_id,
                "permissions": permissions or [],
                "exp": datetime.utcnow()
                + timedelta(minutes=self.settings.JWT_EXPIRE_MINUTES),
                "iat": datetime.utcnow(),
                "type": "access_token",
            }

            token = jwt.encode(
                payload,
                self.settings.JWT_SECRET_KEY,
                algorithm=self.settings.JWT_ALGORITHM,
            )

            # 세션에 저장
            self.active_sessions[token] = {
                "user_id": user_id,
                "permissions": permissions,
                "created_at": datetime.utcnow(),
                "last_activity": datetime.utcnow(),
            }

            return token

        except Exception as e:
            logger.error(f"JWT token creation failed: {e}")
            raise

    def verify_jwt_token(self, token: str) -> Dict[str, Any]:
        """JWT 토큰 검증"""
        try:
            # JWT 검증
            payload = jwt.decode(
                token,
                self.settings.JWT_SECRET_KEY,
                algorithms=[self.settings.JWT_ALGORITHM],
            )

            # 세션 존재 확인
            if token not in self.active_sessions:
                raise ValueError("Session not found")

            # 마지막 활동 시간 업데이트
            self.active_sessions[token]["last_activity"] = datetime.utcnow()

            return payload

        except jwt.ExpiredSignatureError:
            logger.warning("JWT token expired")
            raise
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise
        except Exception as e:
            logger.error(f"Token verification failed: {e}")
            raise

    def revoke_token(self, token: str):
        """토큰 폐기"""
        if token in self.active_sessions:
            del self.active_sessions[token]
            logger.info("Token revoked for user")

    def cleanup_expired_sessions(self):
        """만료된 세션 정리"""
        current_time = datetime.utcnow()
        expired_tokens = []

        for token, session in self.active_sessions.items():
            if current_time - session["last_activity"] > timedelta(
                minutes=self.settings.JWT_EXPIRE_MINUTES
            ):
                expired_tokens.append(token)

        for token in expired_tokens:
            del self.active_sessions[token]

        if expired_tokens:
            logger.info(f"Cleaned up {len(expired_tokens)} expired sessions")


class AuthorizationManager:
    """권한 관리자"""

    def __init__(self):
        self.permissions = {
            "admin": [
                "trading:read",
                "trading:write",
                "trading:delete",
                "portfolio:read",
                "portfolio:write",
                "portfolio:delete",
                "system:read",
                "system:write",
                "system:admin",
                "user:read",
                "user:write",
                "user:delete",
                "config:read",
                "config:write",
                "config:delete",
            ],
            "trader": [
                "trading:read",
                "trading:write",
                "portfolio:read",
                "portfolio:write",
                "system:read",
            ],
            "viewer": ["trading:read", "portfolio:read", "system:read"],
            "analyst": [
                "trading:read",
                "portfolio:read",
                "portfolio:write",
                "system:read",
            ],
        }

    def check_permission(
        self, user_permissions: List[str], required_permission: str
    ) -> bool:
        """권한 확인"""
        return required_permission in user_permissions

    def get_user_permissions(self, user_role: str) -> List[str]:
        """사용자 역할에 따른 권한 목록 반환"""
        return self.permissions.get(user_role, [])


class AuditLogger:
    """감사 로그 관리자"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.logger = logging.getLogger("audit")

        # 감사 로그 핸들러 설정
        handler = logging.FileHandler(os.path.join(settings.LOG_FILE_PATH, "audit.log"))
        formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.INFO)

    def log_trading_action(
        self, user_id: str, action: str, symbol: str, details: Dict[str, Any] = None
    ):
        """트레이딩 활동 로깅"""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "action": action,
            "symbol": symbol,
            "details": details or {},
            "ip_address": self._get_client_ip(),
            "user_agent": self._get_user_agent(),
        }

        self.logger.info(f"TRADING: {json.dumps(log_entry)}")

    def log_system_action(
        self, user_id: str, action: str, details: Dict[str, Any] = None
    ):
        """시스템 활동 로깅"""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "user_id": user_id,
            "action": action,
            "details": details or {},
            "ip_address": self._get_client_ip(),
            "user_agent": self._get_user_agent(),
        }

        self.logger.info(f"SYSTEM: {json.dumps(log_entry)}")

    def log_security_event(
        self, event_type: str, user_id: str = None, details: Dict[str, Any] = None
    ):
        """보안 이벤트 로깅"""
        log_entry = {
            "timestamp": datetime.utcnow().isoformat(),
            "event_type": event_type,
            "user_id": user_id,
            "details": details or {},
            "ip_address": self._get_client_ip(),
            "severity": "HIGH" if "failed" in event_type.lower() else "MEDIUM",
        }

        self.logger.warning(f"SECURITY: {json.dumps(log_entry)}")

    def _get_client_ip(self) -> str:
        """클라이언트 IP 주소 가져오기 (FastAPI context에서)"""
        # 실제 구현에서는 FastAPI Request 객체에서 IP 추출
        return "unknown"

    def _get_user_agent(self) -> str:
        """User-Agent 정보 가져오기"""
        # 실제 구현에서는 FastAPI Request 객체에서 User-Agent 추출
        return "unknown"


class SecurityManager:
    """보안 관리자 - 통합 보안 기능"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.encryption_manager = EncryptionManager(settings.ENCRYPTION_KEY)
        self.auth_manager = AuthenticationManager(settings)
        self.authz_manager = AuthorizationManager()
        self.audit_logger = AuditLogger(settings)

        # 보안 관리자 초기화 로깅
        logger.info("Security Manager initialized")

    def encrypt_sensitive_data(self, data: str) -> str:
        """민감 데이터 암호화"""
        return self.encryption_manager.encrypt_data(data)

    def decrypt_sensitive_data(self, encrypted_data: str) -> str:
        """민감 데이터 복호화"""
        return self.encryption_manager.decrypt_data(encrypted_data)

    def authenticate_user(self, username: str, password: str) -> Optional[str]:
        """사용자 인증"""
        try:
            # 실제 구현에서는 데이터베이스에서 사용자 정보 조회
            # 여기서는 간단한 예시만 보여줌

            if username == "admin" and password == os.environ.get("ADMIN_PASSWORD", ""):
                permissions = self.authz_manager.get_user_permissions("admin")
                token = self.auth_manager.create_jwt_token(username, permissions)

                self.audit_logger.log_security_event(
                    "user_login_success", username, {"role": "admin"}
                )

                return token
            else:
                self.audit_logger.log_security_event(
                    "user_login_failed", username, {"reason": "invalid_credentials"}
                )
                return None

        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            self.audit_logger.log_security_event(
                "user_login_failed", username, {"reason": "system_error"}
            )
            return None

    def authorize_action(self, token: str, required_permission: str) -> bool:
        """활동 권한 확인"""
        try:
            payload = self.auth_manager.verify_jwt_token(token)
            user_permissions = payload.get("permissions", [])

            if self.authz_manager.check_permission(
                user_permissions, required_permission
            ):
                return True
            else:
                self.audit_logger.log_security_event(
                    "unauthorized_access_attempt",
                    payload.get("user_id"),
                    {"required_permission": required_permission},
                )
                return False

        except Exception as e:
            logger.error(f"Authorization failed: {e}")
            return False

    def log_trading_action(
        self, token: str, action: str, symbol: str, details: Dict[str, Any] = None
    ):
        """트레이딩 활동 기록"""
        try:
            payload = self.auth_manager.verify_jwt_token(token)
            user_id = payload.get("user_id")

            self.audit_logger.log_trading_action(user_id, action, symbol, details)

        except Exception as e:
            logger.error(f"Failed to log trading action: {e}")

    def generate_api_signature(
        self, method: str, url: str, params: Dict[str, Any]
    ) -> str:
        """API 서명 생성"""
        return self.encryption_manager.generate_api_signature(
            method, url, params, self.settings.KIS_APP_SECRET
        )


def encrypt_data(data: str, settings: Settings) -> str:
    """데이터 암호화 유틸리티 함수"""
    security_manager = SecurityManager(settings)
    return security_manager.encrypt_sensitive_data(data)


def decrypt_data(encrypted_data: str, settings: Settings) -> str:
    """데이터 복호화 유틸리티 함수"""
    security_manager = SecurityManager(settings)
    return security_manager.decrypt_sensitive_data(encrypted_data)
