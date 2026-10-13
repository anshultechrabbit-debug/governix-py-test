import { createAsyncThunk, createSlice } from "@reduxjs/toolkit";
import { api, hasSession } from "../api/client";
import type { Me } from "../api/types";
import { queryClient } from "../lib/queryClient";

type Status = "restoring" | "authenticated" | "anonymous";

interface AuthState {
  me: Me | null;
  status: Status;
}

const initialState: AuthState = { me: null, status: hasSession() ? "restoring" : "anonymous" };

/** Restore a session after reload from its cookies (the client refreshes an expired access cookie). */
export const restoreSession = createAsyncThunk("auth/restore", async () => api.get<Me>("/auth/me"));

export const login = createAsyncThunk("auth/login", async ({ email, password }: { email: string; password: string }) => {
  await api.post("/auth/login", { email, password }); // sets the session cookies
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
        state.me = action.payload;
        state.status = "authenticated";
      })
      .addCase(logout.fulfilled, (state) => {
        state.me = null;
        state.status = "anonymous";
      });
  },
});

export const { sessionExpired } = authSlice.actions;
export default authSlice.reducer;
