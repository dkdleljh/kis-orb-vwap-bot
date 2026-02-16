"""
Enterprise KIS Trading Bot - Configuration Settings
=====================================

Centralized configuration management for the trading system.
Environment variables, database connections, and system settings.
"""

import os
from typing import Optional, List
from pydantic import BaseSettings, Field

class Settings(BaseSettings):
    """시스템 전체 설정"""
    
    # Application
    APP_NAME: str = "KIS Trading Bot"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = Field(default=False, env="DEBUG")
    LOG_LEVEL: str = Field(default="INFO", env="LOG_LEVEL")
    
    # KIS API Configuration
    KIS_API_URL: str = Field(default="https://openapi.koreainvestment.com:9443", env="KIS_API_URL")
    KIS_WS_URL: str = Field(default="wss://openapi.koreainvestment.com:9443", env="KIS_WS_URL")
    KIS_APP_KEY: str = Field(..., env="KIS_APP_KEY")
    KIS_APP_SECRET: str = Field(..., env="KIS_APP_SECRET")
    
    # Account Information
    ACCOUNT_NUMBER: str = Field(..., env="ACCOUNT_NUMBER")
    ACCOUNT_PRODUCT_CODE: str = Field(default="01", env="ACCOUNT_PRODUCT_CODE")
    
    # Database Configuration
    POSTGRES_HOST: str = Field(default="localhost", env="POSTGRES_HOST")
    POSTGRES_PORT: int = Field(default=5432, env="POSTGRES_PORT")
    POSTGRES_DB: str = Field(default="kis_trading", env="POSTGRES_DB")
    POSTGRES_USER: str = Field(..., env="POSTGRES_USER")
    POSTGRES_PASSWORD: str = Field(..., env="POSTGRES_PASSWORD")
    
    # Redis Configuration
    REDIS_HOST: str = Field(default="localhost", env="REDIS_HOST")
    REDIS_PORT: int = Field(default=6379, env="REDIS_PORT")
    REDIS_DB: int = Field(default=0, env="REDIS_DB")
    REDIS_PASSWORD: Optional[str] = Field(None, env="REDIS_PASSWORD")
    
    @property
    def REDIS_URL(self) -> str:
        auth_part = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth_part}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
    
    # Kafka Configuration
    KAFKA_BOOTSTRAP_SERVERS: List[str] = Field(
        default=["localhost:9092"], 
        env="KAFKA_BOOTSTRAP_SERVERS"
    )
    KAFKA_TOPIC_MARKET_DATA: str = Field(default="market_data", env="KAFKA_TOPIC_MARKET_DATA")
    KAFKA_TOPIC_TRADING_ORDERS: str = Field(default="trading_orders", env="KAFKA_TOPIC_TRADING_ORDERS")
    KAFKA_TOPIC_RISK_ALERTS: str = Field(default="risk_alerts", env="KAFKA_TOPIC_RISK_ALERTS")
    
    # ClickHouse Configuration
    CLICKHOUSE_HOST: str = Field(default="localhost", env="CLICKHOUSE_HOST")
    CLICKHOUSE_PORT: int = Field(default=9000, env="CLICKHOUSE_PORT")
    CLICKHOUSE_DB: str = Field(default="trading_data", env="CLICKHOUSE_DB")
    CLICKHOUSE_USER: str = Field(..., env="CLICKHOUSE_USER")
    CLICKHOUSE_PASSWORD: str = Field(..., env="CLICKHOUSE_PASSWORD")
    
    # Trading Configuration
    MAX_POSITIONS: int = Field(default=100, env="MAX_POSITIONS")
    MAX_POSITION_VALUE: int = Field(default=10000000, env="MAX_POSITION_VALUE")  # 1천만원
    MAX_DAILY_LOSS: int = Field(default=1000000, env="MAX_DAILY_LOSS")  # 100만원
    COMMISSION_RATE: float = Field(default=0.00015, env="COMMISSION_RATE")  # 0.015%
    
    # Strategy Configuration
    ORB_TIME_WINDOW: int = Field(default=300, env="ORB_TIME_WINDOW")  # 5 minutes in seconds
    VWAP_PERIOD: int = Field(default=390, env="VWAP_PERIOD")  # Trading day minutes
    NEWS_SENTIMENT_THRESHOLD: float = Field(default=0.3, env="NEWS_SENTIMENT_THRESHOLD")
    
    # Risk Management
    VAR_CONFIDENCE_LEVEL: float = Field(default=0.95, env="VAR_CONFIDENCE_LEVEL")
    VAR_TIME_HORIZON: int = Field(default=1, env="VAR_TIME_HORIZON")  # days
    MAX_LEVERAGE: float = Field(default=2.0, env="MAX_LEVERAGE")
    
    # Performance Configuration
    MAX_CONCURRENT_CONNECTIONS: int = Field(default=100, env="MAX_CONCURRENT_CONNECTIONS")
    REQUEST_TIMEOUT: int = Field(default=30, env="REQUEST_TIMEOUT")
    BATCH_SIZE: int = Field(default=100, env="BATCH_SIZE")
    
    # Security
    JWT_SECRET_KEY: str = Field(..., env="JWT_SECRET_KEY")
    JWT_ALGORITHM: str = Field(default="HS256", env="JWT_ALGORITHM")
    JWT_EXPIRE_MINUTES: int = Field(default=1440, env="JWT_EXPIRE_MINUTES")  # 24 hours
    
    # Encryption
    ENCRYPTION_KEY: str = Field(..., env="ENCRYPTION_KEY")
    
    # Monitoring & Logging
    METRICS_PORT: int = Field(default=9090, env="METRICS_PORT")
    LOG_FILE_PATH: str = Field(default="/var/log/kis_trading", env="LOG_FILE_PATH")
    
    # Backup & Recovery
    BACKUP_SCHEDULE: str = Field(default="0 2 * * *", env="BACKUP_SCHEDULE")  # Daily at 2 AM
    BACKUP_RETENTION_DAYS: int = Field(default=30, env="BACKUP_RETENTION_DAYS")
    
    # API Gateway Configuration
    API_RATE_LIMIT_PER_SECOND: int = Field(default=10, env="API_RATE_LIMIT_PER_SECOND")
    API_RATE_LIMIT_PER_MINUTE: int = Field(default=200, env="API_RATE_LIMIT_PER_MINUTE")
    CIRCUIT_BREAKER_THRESHOLD: int = Field(default=5, env="CIRCUIT_BREAKER_THRESHOLD")
    CIRCUIT_BREAKER_TIMEOUT: int = Field(default=60, env="CIRCUIT_BREAKER_TIMEOUT")
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True

class DevelopmentSettings(Settings):
    """개발 환경 설정"""
    DEBUG: bool = True
    LOG_LEVEL: str = "DEBUG"

class ProductionSettings(Settings):
    """프로덕션 환경 설정"""
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"
    
    # Production-specific security settings
    KAFKA_BOOTSTRAP_SERVERS: List[str] = ["kafka1:9092", "kafka2:9092", "kafka3:9092"]
    REDIS_HOST: str = "redis-cluster"
    POSTGRES_HOST: str = "postgres-primary"
    CLICKHOUSE_HOST: str = "clickhouse-cluster"

class TestSettings(Settings):
    """테스트 환경 설정"""
    DEBUG: bool = True
    LOG_LEVEL: str = "DEBUG"
    
    # Test database configuration
    POSTGRES_DB: str = "kis_trading_test"
    REDIS_DB: int = 1
    CLICKHOUSE_DB: str = "trading_data_test"
    
    # Test API endpoints
    KIS_API_URL: str = "https://test-api.koreainvestment.com:9443"
    KIS_WS_URL: str = "wss://test-api.koreainvestment.com:9443"

def get_settings() -> Settings:
    """환경에 맞는 설정 객체 반환"""
    environment = os.getenv("ENVIRONMENT", "development").lower()
    
    if environment == "production":
        return ProductionSettings()
    elif environment == "test":
        return TestSettings()
    else:
        return DevelopmentSettings()