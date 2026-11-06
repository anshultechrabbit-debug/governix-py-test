import { useCallback } from "react";
import { useDispatch, useSelector } from "react-redux";
import { ApiError } from "../api/client";
import type { AppDispatch, RootState } from ".";
import { login, logout, setMfaEnabled, verifyMfa } from "./authSlice";
import { dismissToast, pushToast, type ToastTone } from "./toastSlice";

export const useAppDispatch = useDispatch.withTypes<AppDispatch>();
export const useAppSelector = useSelector.withTypes<RootState>();

const TOAST_MS = 5000;

/** A thunk's unwrap() rejects with a plain copy of the error (Redux serializes it), not the ApiError itself, so
 * `instanceof ApiError` failed and pages showed their generic text instead of the server's message ("Your account
 * is suspended. Contact ..."). Turn it back into an ApiError, keeping the server's reason and message. */
function asApiError(error: unknown): never {
  if (error instanceof ApiError) throw error;
  const copy = (error ?? {}) as { message?: string; code?: string };
  throw new ApiError(0, copy.code ?? "ERROR", copy.message || "Something went wrong. Please try again.");
}

/** Convenience hook over the auth slice. */
export function useAuth() {
  const dispatch = useAppDispatch();
  const { me, status } = useAppSelector((state) => state.auth);
  const can = useCallback(
    (...permissions: string[]) => Boolean(me) && permissions.every((p) => me!.permissions.includes(p)),
    [me],
  );
  return {
    me,
    loading: status === "restoring",
    can,
    /** Resolves to null when the account needs its authenticator code next (`verifyMfa`). */
    login: (email: string, password: string) => dispatch(login({ email, password })).unwrap().catch(asApiError),
    verifyMfa: (code: string) => dispatch(verifyMfa(code)).unwrap().catch(asApiError),
    setMfaEnabled: (enabled: boolean) => dispatch(setMfaEnabled(enabled)),
    logout: () => dispatch(logout()).unwrap(),
  };
}

export function useToast() {
  const dispatch = useAppDispatch();
  return useCallback(
    (tone: ToastTone, message: string) => {
      const { payload } = dispatch(pushToast(tone, message));
      setTimeout(() => dispatch(dismissToast(payload.id)), TOAST_MS);
    },
    [dispatch],
  );
}
