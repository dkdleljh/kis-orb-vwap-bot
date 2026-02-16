export const formatCurrency = (value: number): string => {
  return new Intl.NumberFormat('ko-KR', {
    style: 'currency',
    currency: 'KRW',
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
};

export const formatPercentage = (value: number): string => {
  return `${value.toFixed(2)}%`;
};

export const formatNumber = (value: number): string => {
  return new Intl.NumberFormat('ko-KR').format(value);
};

export const formatDateTime = (date: string | Date): string => {
  const dateObj = typeof date === 'string' ? new Date(date) : date;
  return new Intl.DateTimeFormat('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(dateObj);
};

export const formatTime = (date: string | Date): string => {
  const dateObj = typeof date === 'string' ? new Date(date) : date;
  return new Intl.DateTimeFormat('ko-KR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(dateObj);
};

export const getChangeColor = (value: number): string => {
  if (value > 0) return '#4caf50';
  if (value < 0) return '#f44336';
  return '#757575';
};

export const getRiskLevelColor = (level: string): string => {
  switch (level) {
    case 'LOW':
      return '#4caf50';
    case 'MEDIUM':
      return '#ff9800';
    case 'HIGH':
      return '#f44336';
    case 'CRITICAL':
      return '#d32f2f';
    default:
      return '#757575';
  }
};

export const getSignalColor = (signal: string): string => {
  switch (signal) {
    case 'BUY':
      return '#4caf50';
    case 'SELL':
      return '#f44336';
    case 'HOLD':
      return '#757575';
    default:
      return '#757575';
  }
};