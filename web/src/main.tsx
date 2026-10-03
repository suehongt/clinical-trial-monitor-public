import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import "./styles/tokens.css";
import "./styles/primitives.css";
import "./styles/accessibility.css";
import "./styles.css";
import "./styles/shell.css";
import "./styles/trials.css";
import "./styles/trial-detail.css";
import "./styles/intelligence.css";
import "./styles/operations.css";

try {
  document.documentElement.dataset.theme = localStorage.getItem("ct-theme") === "dark" ? "dark" : "light";
} catch {
  document.documentElement.dataset.theme = "light";
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
