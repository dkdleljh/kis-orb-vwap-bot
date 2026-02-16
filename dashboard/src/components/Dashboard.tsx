import React, { useState, useEffect } from 'react';
import {
  Grid,
  Card,
  CardContent,
  Typography,
  Box,
  LinearProgress,
  Chip,
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
} from '@mui/material';
import {
  TrendingUp,
  TrendingDown,
  AccountBalance,
  Warning,
  AccessTime,
} from '@mui/icons-material';
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
} from 'recharts';
import { useQuery } from 'react-query';
import { useWebSocket } from '../contexts/WebSocketContext.tsx';
import { formatCurrency, formatPercentage } from '../utils/formatters.ts';

const Dashboard: React.FC = () => {
  const [portfolioData, setPortfolioData] = useState<any>({});
  const [performanceData, setPerformanceData] = useState<any[]>([]);
  const { lastMessage } = useWebSocket();

  // API 데이터 조회
  const { data: portfolioOverview } = useQuery(
    'portfolio-overview',
    async () => {
      const response = await fetch('/portfolio/overview', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to fetch portfolio data');
      return response.json();
    },
    {
      refetchInterval: 5000, // 5초마다 리프레시
    }
  );

  const { data: performanceMetrics } = useQuery(
    'performance-metrics',
    async () => {
      const response = await fetch('/performance/metrics', {
        headers: {
          'Authorization': `Bearer ${localStorage.getItem('token')}`,
        },
      });
      if (!response.ok) throw new Error('Failed to fetch performance data');
      return response.json();
    },
    {
      refetchInterval: 10000, // 10초마다 리프레시
    }
  );

  // WebSocket 메시지 처리
  useEffect(() => {
    if (lastMessage) {
      const message = JSON.parse(lastMessage.data);
      if (message.type === 'portfolio_update') {
        setPortfolioData(message.data);
      } else if (message.type === 'performance_update') {
        // 성과 데이터 업데이트 로직
      }
    }
  }, [lastMessage]);

  // 포트폴리오 데이터 업데이트
  useEffect(() => {
    if (portfolioOverview?.data) {
      setPortfolioData(portfolioOverview.data);
    }
  }, [portfolioOverview]);

  // 가상의 성과 데이터 (실제로는 API에서 가져와야 함)
  const mockPerformanceData = [
    { name: '1월', value: 1000000, profit: 20000 },
    { name: '2월', value: 1050000, profit: 50000 },
    { name: '3월', value: 1120000, profit: 70000 },
    { name: '4월', value: 1080000, profit: -40000 },
    { name: '5월', value: 1150000, profit: 70000 },
    { name: '6월', value: 1200000, profit: 50000 },
  ];

  // 포트폴리오 구성 데이터
  const portfolioComposition = portfolioData?.positions?.map((position: any) => ({
    name: position.symbol,
    value: position.market_value,
  })) || [];

  const COLORS = ['#0088FE', '#00C49F', '#FFBB28', '#FF8042', '#8884D8'];

  return (
    <Box>
      <Typography variant="h4" gutterBottom>
        대시보드
      </Typography>
      
      {/* 주요 메트릭 */}
      <Grid container spacing={3} sx={{ mb: 3 }}>
        <Grid item xs={12} sm={6} md={3}>
          <Card>
            <CardContent>
              <Box display="flex" alignItems="center">
                <AccountBalance sx={{ mr: 2, color: 'primary.main' }} />
                <Box>
                  <Typography color="textSecondary" gutterBottom>
                    포트폴리오 가치
                  </Typography>
                  <Typography variant="h5">
                    {formatCurrency(portfolioData?.metrics?.portfolio_value || 0)}
                  </Typography>
                </Box>
              </Box>
            </CardContent>
          </Card>
        </Grid>
        
        <Grid item xs={12} sm={6} md={3}>
          <Card>
            <CardContent>
              <Box display="flex" alignItems="center">
                {portfolioData?.metrics?.daily_pnl >= 0 ? (
                  <TrendingUp sx={{ mr: 2, color: 'success.main' }} />
                ) : (
                  <TrendingDown sx={{ mr: 2, color: 'error.main' }} />
                )}
                <Box>
                  <Typography color="textSecondary" gutterBottom>
                    일일 손익
                  </Typography>
                  <Typography 
                    variant="h5" 
                    color={portfolioData?.metrics?.daily_pnl >= 0 ? 'success.main' : 'error.main'}
                  >
                    {formatCurrency(portfolioData?.metrics?.daily_pnl || 0)}
                  </Typography>
                </Box>
              </Box>
            </CardContent>
          </Card>
        </Grid>
        
        <Grid item xs={12} sm={6} md={3}>
          <Card>
            <CardContent>
              <Box display="flex" alignItems="center">
                <Warning sx={{ mr: 2, color: 'warning.main' }} />
                <Box>
                  <Typography color="textSecondary" gutterBottom>
                    VaR (95%)
                  </Typography>
                  <Typography variant="h5">
                    {formatCurrency(Math.abs(portfolioData?.metrics?.var_95_1day || 0))}
                  </Typography>
                </Box>
              </Box>
            </CardContent>
          </Card>
        </Grid>
        
        <Grid item xs={12} sm={6} md={3}>
          <Card>
            <CardContent>
              <Box display="flex" alignItems="center">
                <TrendingUp sx={{ mr: 2, color: 'info.main' }} />
                <Box>
                  <Typography color="textSecondary" gutterBottom>
                    샤프 비율
                  </Typography>
                  <Typography variant="h5">
                    {(portfolioData?.metrics?.sharpe_ratio || 0).toFixed(2)}
                  </Typography>
                </Box>
              </Box>
            </CardContent>
          </Card>
        </Grid>
      </Grid>

      {/* 차트 섹션 */}
      <Grid container spacing={3}>
        {/* 포트폴리오 성과 차트 */}
        <Grid item xs={12} md={8}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                포트폴리오 성과
              </Typography>
              <ResponsiveContainer width="100%" height={300}>
                <LineChart data={mockPerformanceData}>
                  <CartesianGrid strokeDasharray="3 3" />
                  <XAxis dataKey="name" />
                  <YAxis />
                  <Tooltip formatter={(value) => formatCurrency(value as number)} />
                  <Line 
                    type="monotone" 
                    dataKey="value" 
                    stroke="#8884d8" 
                    strokeWidth={2}
                    name="포트폴리오 가치"
                  />
                </LineChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </Grid>
        
        {/* 포트폴리오 구성 */}
        <Grid item xs={12} md={4}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                포트폴리오 구성
              </Typography>
              <ResponsiveContainer width="100%" height={300}>
                <PieChart>
                  <Pie
                    data={portfolioComposition}
                    cx="50%"
                    cy="50%"
                    labelLine={false}
                    label={({ name, percent }) => `${name} ${(percent * 100).toFixed(0)}%`}
                    outerRadius={80}
                    fill="#8884d8"
                    dataKey="value"
                  >
                    {portfolioComposition.map((entry, index) => (
                      <Cell key={`cell-${index}`} fill={COLORS[index % COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip formatter={(value) => formatCurrency(value as number)} />
                </PieChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>
        </Grid>
        
        {/* 최근 거래 내역 */}
        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                최근 포지션
              </Typography>
              <List>
                {portfolioData?.positions?.slice(0, 5).map((position: any, index: number) => (
                  <ListItem key={index}>
                    <ListItemIcon>
                      {position.unrealized_pnl >= 0 ? (
                        <TrendingUp color="success" />
                      ) : (
                        <TrendingDown color="error" />
                      )}
                    </ListItemIcon>
                    <ListItemText
                      primary={position.symbol}
                      secondary={`${formatCurrency(position.market_value)} | ${formatPercentage(position.pnl_percentage)}`}
                    />
                    <Chip
                      label={position.unrealized_pnl >= 0 ? '수익' : '손실'}
                      color={position.unrealized_pnl >= 0 ? 'success' : 'error'}
                      size="small"
                    />
                  </ListItem>
                ))}
              </List>
            </CardContent>
          </Card>
        </Grid>
        
        {/* 시스템 상태 */}
        <Grid item xs={12} md={6}>
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                시스템 상태
              </Typography>
              <Box>
                <Typography variant="body2" gutterBottom>
                  API 연결 상태
                </Typography>
                <LinearProgress variant="determinate" value={100} sx={{ mb: 2 }} />
                
                <Typography variant="body2" gutterBottom>
                  데이터 수집
                </Typography>
                <LinearProgress variant="determinate" value={95} sx={{ mb: 2 }} />
                
                <Typography variant="body2" gutterBottom>
                  전략 엔진
                </Typography>
                <LinearProgress variant="determinate" value={100} sx={{ mb: 2 }} />
                
                <Typography variant="body2" gutterBottom>
                  리스크 관리
                </Typography>
                <LinearProgress variant="determinate" value={85} />
              </Box>
            </CardContent>
          </Card>
        </Grid>
      </Grid>
    </Box>
  );
};

export default Dashboard;