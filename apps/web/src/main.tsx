import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import App from "./App";
import { AuthGate } from "./auth/AuthGate";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Application root was not found.");

createRoot(root).render(
  <StrictMode>
    <AuthGate>
      <App />
    </AuthGate>
  </StrictMode>,
);
