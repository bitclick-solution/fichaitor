import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./app.css";
import { iniciarScrollSuave } from "./movimiento";

iniciarScrollSuave();

const raiz = document.getElementById("root");
if (!raiz) throw new Error("Falta el elemento #root");

createRoot(raiz).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
