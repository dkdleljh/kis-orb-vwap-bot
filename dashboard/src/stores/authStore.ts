import { create } from 'zustand';

export const useAuthStore = create((set) => ({
  isAuthenticated: true,
  user: { id: '1', name: 'Trader' },
  login: () => set({ isAuthenticated: true }),
  logout: () => set({ isAuthenticated: false }),
}));
