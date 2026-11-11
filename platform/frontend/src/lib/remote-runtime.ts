export type RemoteServer = {
  id: string;
  name: string;
  host: string;
  username: string;
  port: number | null;
  auth_method: "config" | "key" | "password";
  identity_file: string;
  fingerprint: string;
  runtime_port: number;
  version: string;
  service_manager: string;
  connected: boolean;
  enabled?: boolean;
};

export type RemoteServerInput = Pick<RemoteServer,
  "name" | "host" | "username" | "port" | "auth_method" | "identity_file"> & {
  server_id?: string;
  password?: string;
  passphrase?: string;
  fingerprint?: string;
  remember_secret: boolean;
};

export type RuntimeOperation = {
  id: string;
  phase: "connecting" | "installing" | "checking" | "ready" | "host_key" | "failed";
  done: boolean;
  fingerprint?: string;
  error?: string;
  server?: RemoteServer;
};

export type RemoteAvailability = {
  state: "online" | "unavailable" | "disconnected" | "authentication_required"
    | "verification_required" | "incompatible";
  checked_at: string;
};

export function availabilityPollInterval(state?: RemoteAvailability["state"]) {
  return state === "online" || state === undefined ? 5_000 : 15_000;
}

export function canKeepCachedSession(session: unknown, messages: unknown) {
  return session !== undefined && session !== null && messages !== undefined && messages !== null;
}

/** Root paths are scoped to one machine; changing machines clears a stale choice. */
export function changeProjectRuntime(home: string) {
  return { home, root_folder: null as string | null };
}
