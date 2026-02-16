"""
Enterprise KIS Trading Bot - Main Application
===============================================

Main entry point for the enterprise-grade KIS trading system.
Orchestrates all components and provides unified interface.

Features:
- Component initialization and dependency injection
- Graceful shutdown handling
- Health monitoring and logging
- Production-ready configuration
"""

import asyncio
import signal
import sys
import logging
from typing import Dict, Any

from src.config.settings import get_settings, Settings
from src.utils.logger import setup_logging
from src.dashboard.dashboard_api import set_dashboard_components
from src.data_pipeline.data_pipeline import DataPipeline
from src.strategy_engine.strategy_engine import StrategyEngine
from src.risk_management.risk_manager import RiskManager
from src.monitoring.metrics import MonitoringService
from src.security.encryption import SecurityManager
from src.backtesting.backtesting_engine import BacktestingEngine

logger = logging.getLogger(__name__)

class TradingSystem:
    """메인 트레이딩 시스템"""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.running = False
        
        # Initialize components (will be started in start())
        self.data_pipeline: DataPipeline = None
        self.strategy_engine: StrategyEngine = None
        self.risk_manager: RiskManager = None
        self.monitoring_service: MonitoringService = None
        self.security_manager: SecurityManager = None
        self.backtesting_engine: BacktestingEngine = None
        
        # Background tasks
        self.background_tasks = set()
    
    async def initialize(self):
        """모든 컴포넌트 초기화"""
        try:
            logger.info("Initializing trading system components...")
            
            # Security Manager (first)
            self.security_manager = SecurityManager(self.settings)
            logger.info("Security manager initialized")
            
            # Data Pipeline
            self.data_pipeline = DataPipeline(self.settings)
            await self.data_pipeline.start()
            logger.info("Data pipeline initialized")
            
            # Strategy Engine
            self.strategy_engine = StrategyEngine(self.settings, self.data_pipeline)
            await self.strategy_engine.initialize()
            logger.info("Strategy engine initialized")
            
            # Risk Manager
            self.risk_manager = RiskManager(self.settings, self.data_pipeline)
            logger.info("Risk manager initialized")
            
            # Monitoring Service
            self.monitoring_service = MonitoringService(self.settings)
            await self.monitoring_service.start_monitoring()
            logger.info("Monitoring service initialized")
            
            # Backtesting Engine
            self.backtesting_engine = BacktestingEngine(self.settings, self.data_pipeline)
            logger.info("Backtesting engine initialized")
            
            # Connect components to dashboard
            set_dashboard_components(
                api_gateway=None,  # Will be set separately if needed
                data_pipeline=self.data_pipeline,
                strategy_engine=self.strategy_engine,
                risk_manager=self.risk_manager,
                monitoring_service=self.monitoring_service
            )
            
            logger.info("All components initialized successfully")
            
        except Exception as e:
            logger.error(f"Failed to initialize trading system: {e}")
            raise
    
    async def start(self):
        """트레이딩 시스템 시작"""
        if self.running:
            logger.warning("Trading system is already running")
            return
        
        try:
            logger.info("Starting enterprise KIS trading system...")
            await self.initialize()
            
            # Start main trading loop
            task = asyncio.create_task(self._trading_loop())
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
            
            # Start monitoring tasks
            task = asyncio.create_task(self._monitoring_loop())
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
            
            self.running = True
            logger.info("Enterprise KIS trading system started successfully")
            
        except Exception as e:
            logger.error(f"Failed to start trading system: {e}")
            raise
    
    async def stop(self):
        """트레이딩 시스템 중지"""
        if not self.running:
            return
        
        logger.info("Stopping trading system...")
        self.running = False
        
        # Cancel background tasks
        for task in self.background_tasks:
            task.cancel()
        
        # Stop components
        if self.data_pipeline:
            await self.data_pipeline.stop()
        
        if self.monitoring_service:
            await self.monitoring_service.stop_monitoring()
        
        logger.info("Trading system stopped")
    
    async def _trading_loop(self):
        """메인 트레이딩 루프"""
        while self.running:
            try:
                # Get latest market data and process signals
                # This is a simplified version - actual implementation would
                # subscribe to real-time market data streams
                await asyncio.sleep(1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in trading loop: {e}")
                await asyncio.sleep(5)
    
    async def _monitoring_loop(self):
        """모니터링 루프"""
        while self.running:
            try:
                # Periodic health checks and metrics collection
                if self.risk_manager:
                    risk_summary = self.risk_manager.get_risk_summary()
                    logger.debug(f"Risk level: {risk_summary.get('risk_level')}")
                
                await asyncio.sleep(30)  # Check every 30 seconds
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                await asyncio.sleep(10)
    
    def get_system_status(self) -> Dict[str, Any]:
        """시스템 상태 반환"""
        return {
            'running': self.running,
            'components': {
                'data_pipeline': self.data_pipeline is not None,
                'strategy_engine': self.strategy_engine is not None,
                'risk_manager': self.risk_manager is not None,
                'monitoring_service': self.monitoring_service is not None,
                'backtesting_engine': self.backtesting_engine is not None
            },
            'settings': {
                'environment': self.settings.ENVIRONMENT,
                'log_level': self.settings.LOG_LEVEL
            }
        }

# Global system instance
trading_system: TradingSystem = None

async def main():
    """메인 함수"""
    global trading_system
    
    try:
        # Setup logging
        settings = get_settings()
        setup_logging(
            log_level=settings.LOG_LEVEL,
            log_file_path=settings.LOG_FILE_PATH,
            enable_json=True,
            enable_console=True
        )
        
        logger.info("Enterprise KIS Trading Bot v2.0.0")
        logger.info(f"Environment: {settings.ENVIRONMENT}")
        
        # Create and start trading system
        trading_system = TradingSystem(settings)
        
        # Setup signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            logger.info(f"Received signal {signum}, initiating shutdown...")
            asyncio.create_task(trading_system.stop())
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # Start the system
        await trading_system.start()
        
        # Keep the system running
        while trading_system.running:
            await asyncio.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error in main: {e}")
        sys.exit(1)
    finally:
        if trading_system:
            await trading_system.stop()
        logger.info("Enterprise KIS Trading Bot shutdown complete")

if __name__ == "__main__":
    # Run the application
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
    except Exception as e:
        logger.error(f"Application failed: {e}")
        sys.exit(1)