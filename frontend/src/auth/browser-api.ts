import type { ApiResult } from "../api/client";
import { ApiClient } from "../api/client";
import type { BrowserSessionView, OperationResponse } from "../api/contracts";
import { parseBrowserSessionView, parseOperationResponse } from "../api/validation";

export type BrowserPrincipal = "pm" | "stakeholder";

export interface BrowserSessionApi {
  inspect(principal: BrowserPrincipal): Promise<ApiResult<BrowserSessionView>>;
  activatePm(bootstrapToken: string): Promise<ApiResult<BrowserSessionView>>;
  activateStakeholder(invitationToken: string): Promise<ApiResult<BrowserSessionView>>;
  logout(principal: BrowserPrincipal): Promise<ApiResult<OperationResponse>>;
}

export class HttpBrowserSessionApi implements BrowserSessionApi {
  readonly #client: ApiClient;

  constructor(client = new ApiClient()) {
    this.#client = client;
  }

  async inspect(principal: BrowserPrincipal): Promise<ApiResult<BrowserSessionView>> {
    return await this.#client.result(
      `/api/v1/browser/auth/session?principal=${principal}`,
      { method: "GET" },
      parseBrowserSessionView,
    );
  }

  async activatePm(bootstrapToken: string): Promise<ApiResult<BrowserSessionView>> {
    return await this.#client.result(
      "/api/v1/browser/auth/pm/activate",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bootstrap_token: bootstrapToken }),
      },
      parseBrowserSessionView,
    );
  }

  async activateStakeholder(invitationToken: string): Promise<ApiResult<BrowserSessionView>> {
    return await this.#client.result(
      "/api/v1/browser/auth/stakeholder/activate",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ invitation_token: invitationToken }),
      },
      parseBrowserSessionView,
    );
  }

  async logout(principal: BrowserPrincipal): Promise<ApiResult<OperationResponse>> {
    return await this.#client.result(
      `/api/v1/browser/auth/logout?principal=${principal}`,
      { method: "POST" },
      parseOperationResponse,
    );
  }
}

export const browserSessionApi: BrowserSessionApi = new HttpBrowserSessionApi();
