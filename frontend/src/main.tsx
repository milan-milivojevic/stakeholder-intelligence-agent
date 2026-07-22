import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/app";
import { browserSessionApi } from "./auth/browser-api";
import { prepareBrowserBootstrap } from "./auth/bootstrap";
import "./styles.css";

const rootElement = document.querySelector<HTMLElement>("#root");

if (rootElement === null) {
  throw new Error("The application root is unavailable.");
}

const bootstrap = prepareBrowserBootstrap(window.location, browserSessionApi);

createRoot(rootElement).render(
  <StrictMode>
    <App bootstrap={bootstrap} />
  </StrictMode>,
);
