import React, { createContext, useContext, useEffect, useState } from 'react';

interface WebSocketContextType {
  connected: boolean;
  sendMessage: (msg: string) => void;
}

const WebSocketContext = createContext<WebSocketContextType>({
  connected: false,
  sendMessage: () => {},
});

export const useWebSocket = () => useContext(WebSocketContext);

export const WebSocketProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    setConnected(true);
  }, []);

  const sendMessage = (msg: string) => {
    console.log('WS:', msg);
  };

  return (
    <WebSocketContext.Provider value={{ connected, sendMessage }}>
      {children}
    </WebSocketContext.Provider>
  );
};
