import React from 'react';
import { Box, Button, TextField, Typography, Card, CardContent } from '@mui/material';
import { useAuthStore } from '../stores/authStore.ts';

const Login: React.FC = () => {
  const { login } = useAuthStore();

  return (
    <Box display="flex" justifyContent="center" alignItems="center" minHeight="100vh">
      <Card sx={{ minWidth: 300, p: 2 }}>
        <CardContent>
          <Typography variant="h5" gutterBottom>KIS Trading Bot</Typography>
          <Typography variant="body2" color="text.secondary" mb={2}>Login to Dashboard</Typography>
          <TextField fullWidth label="Username" margin="normal" defaultValue="admin" />
          <TextField fullWidth label="Password" type="password" margin="normal" defaultValue="password" />
          <Button fullWidth variant="contained" sx={{ mt: 2 }} onClick={login}>
            Login
          </Button>
        </CardContent>
      </Card>
    </Box>
  );
};

export default Login;
