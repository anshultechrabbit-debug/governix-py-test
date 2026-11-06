import { createAsyncThunk, createSlice } from "@reduxjs/toolkit";
import { api, hasSession } from "../api/client";
import type { AuthSession, Me } from "../api/types";
import { queryClient } from "../lib/queryClient";

type Status = "restoring" | "authenticated" | "anonymous";

interface AuthState {
  me: Me | null;
  status: Status;
}

const initialState: AuthState = { me: null, status: hasSession() ? "restoring" : "anonymous" };

/** Restore a session after reload from its cookies (the client refreshes an expired access cookie). */
export const restoreSession = createAsyncThunk("auth/restore", async () => api.get<Me>("/auth/me"));

/** Signs in, or resolves to null when the account uses two-step sign-in: then `verifyMfa` with the code. */
export const login = createAsyncThunk("auth/login", async ({ email, password }: { email: string; password: string }) => {
  const session = await api.post<AuthSession>("/auth/login", { email, password }); // sets the session cookies
  if (session.mfa_required) return null;
  return api.get<Me>("/auth/me");
});

/** The code step: a code from the authenticator app, or a backup code. */
export const verifyMfa = createAsyncThunk("auth/verifyMfa", async (code: string) => {
  await api.post<AuthSession>("/auth/mfa/verify", { code });
  return api.get<Me>("/auth/me");
});

export const logout = createAsyncThunk("auth/logout", async () => {
  await api.post("/auth/logout").catch(() => undefined); // revokes the session and clears its cookies
  queryClient.clear();
});

const authSlice = createSlice({
  name: "auth",
  initialState,
  reducers: {
    /** After turning two-step sign-in on or off in the account menu. */
    setMfaEnabled(state, action: { payload: boolean }) {
      if (state.me) state.me.mfa_enabled = action.payload;
    },
    sessionExpired(state) {
      queryClient.clear();
      state.me = null;
      state.status = "anonymous";
    },
  },
  extraReducers: (builder) => {
    builder
      .addCase(restoreSession.fulfilled, (state, action) => {
        state.me = action.payload;
        state.status = "authenticated";
      })
      .addCase(restoreSession.rejected, (state) => {
        state.me = null;
        state.status = "anonymous";
      })
      .addCase(login.fulfilled, (state, action) => {
        if (!action.payload) return; // the code step comes first
        state.me = action.payload;
        state.status = "authenticated";
      })
      .addCase(verifyMfa.fulfilled, (state, action) => {
        state.me = action.payload;
        state.status = "authenticated";
      })
      .addCase(logout.fulfilled, (state) => {
        state.me = null;
        state.status = "anonymous";
      });
  },
});

export const { sessionExpired, setMfaEnabled } = authSlice.actions;
export default authSlice.reducer;
