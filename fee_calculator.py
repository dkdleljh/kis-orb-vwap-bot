"""
Fee and Slippage Calculator - 정확한 수익 계산을 위한 수수료/슬리피지 모듈.
"""

from dataclasses import dataclass


@dataclass
class TradeCosts:
    commission: float
    slippage: float
    tax: float
    total_cost: float
    
    def net_pnl(self, gross_pnl: float) -> float:
        return gross_pnl - self.total_cost


class FeeCalculator:
    def __init__(
        self,
        commission_rate: float = 0.00015,
        commission_min: float = 1000,
        slippage_rate: float = 0.001,
        tax_rate: float = 0.002,
        is_overseas: bool = False,
        min_commission_check: bool = True,
    ):
        self.commission_rate = commission_rate
        self.commission_min = commission_min
        self.slippage_rate = slippage_rate
        self.tax_rate = tax_rate
        self.is_overseas = is_overseas
        self.min_commission_check = min_commission_check
    
    def calculate_entry_cost(self, price: float, qty: int) -> TradeCosts:
        gross = price * qty
        
        commission = gross * self.commission_rate
        if self.min_commission_check:
            commission = max(commission, self.commission_min)
        
        slippage = price * qty * self.slippage_rate
        
        tax = 0.0
        
        total = commission + slippage + tax
        
        return TradeCosts(
            commission=commission,
            slippage=slippage,
            tax=tax,
            total_cost=total,
        )
    
    def calculate_exit_cost(self, price: float, qty: int) -> TradeCosts:
        gross = price * qty
        
        commission = gross * self.commission_rate
        if self.min_commission_check:
            commission = max(commission, self.commission_min)
        
        slippage = price * qty * self.slippage_rate
        
        tax = gross * self.tax_rate if not self.is_overseas else 0.0
        
        total = commission + slippage + tax
        
        return TradeCosts(
            commission=commission,
            slippage=slippage,
            tax=tax,
            total_cost=total,
        )
    
    def calculate_total_roundtrip(
        self,
        entry_price: float,
        exit_price: float,
        qty: int,
    ) -> TradeCosts:
        entry = self.calculate_entry_cost(entry_price, qty)
        exit = self.calculate_exit_cost(exit_price, qty)
        
        return TradeCosts(
            commission=entry.commission + exit.commission,
            slippage=entry.slippage + exit.slippage,
            tax=entry.tax + exit.tax,
            total_cost=entry.total_cost + exit.total_cost,
        )
    
    def get_net_profit(
        self,
        entry_price: float,
        exit_price: float,
        qty: int,
    ) -> float:
        gross_pnl = (exit_price - entry_price) * qty
        costs = self.calculate_total_roundtrip(entry_price, exit_price, qty)
        return gross_pnl - costs.total_cost
    
    def get_net_pnl_percent(
        self,
        entry_price: float,
        exit_price: float,
    ) -> float:
        if entry_price <= 0:
            return 0.0
        
        gross_pnl_pct = (exit_price - entry_price) / entry_price
        
        entry_cost_pct = self.commission_rate + self.slippage_rate
        exit_cost_pct = self.commission_rate + self.slippage_rate + self.tax_rate
        
        net_pnl_pct = gross_pnl_pct - entry_cost_pct - exit_cost_pct
        
        return net_pnl_pct
    
    def calculate_breakeven_price(
        self,
        entry_price: float,
    ) -> float:
        entry_cost_multiplier = 1 + self.commission_rate + self.slippage_rate
        exit_cost_multiplier = 1 + self.commission_rate + self.slippage_rate + self.tax_rate
        
        breakeven = entry_price * entry_cost_multiplier * exit_cost_multiplier
        
        return breakeven
    
    def get_min_profitable_move(self, price: float) -> float:
        costs = self.calculate_total_roundtrip(price, price, 1)
        return costs.total_cost


class USFeeCalculator(FeeCalculator):
    def __init__(
        self,
        commission_per_share: float = 0.005,
        slippage_rate: float = 0.001,
    ):
        super().__init__(
            commission_rate=0,
            commission_min=0,
            slippage_rate=slippage_rate,
            tax_rate=0,
            is_overseas=True,
        )
        self.commission_per_share = commission_per_share
    
    def calculate_entry_cost(self, price: float, qty: int) -> TradeCosts:
        commission = qty * self.commission_per_share
        slippage = price * qty * self.slippage_rate
        
        total = commission + slippage
        
        return TradeCosts(
            commission=commission,
            slippage=slippage,
            tax=0,
            total_cost=total,
        )
    
    def calculate_exit_cost(self, price: float, qty: int) -> TradeCosts:
        return self.calculate_entry_cost(price, qty)
