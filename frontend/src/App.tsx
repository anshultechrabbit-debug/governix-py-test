import { lazy, useEffect, useRef, type ComponentType, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router";
import { Spinner, Toaster } from "./components/ui";
import { AppLayout } from "./components/layout/AppLayout";
import { LoginPage } from "./pages/LoginPage";
import { NotFoundPage } from "./pages/NotFoundPage";
import { ForgotPasswordPage, ResetPasswordPage } from "./pages/PasswordResetPages";
import { restoreSession } from "./store/authSlice";
import { useAppDispatch, useAuth } from "./store/hooks";

// Each page is its own chunk, fetched the first time it is opened, so sign-in does not wait for the whole app.
function page<M, K extends keyof M>(load: () => Promise<M>, name: K) {
  return lazy(async () => ({ default: (await load())[name] as ComponentType }));
}
const AdminUsersPage = page(() => import("./pages/AdminUsersPage"), "AdminUsersPage");
const AssignDocumentsPage = page(() => import("./pages/AssignDocumentsPage"), "AssignDocumentsPage");
const AssistantPage = page(() => import("./pages/AssistantPage"), "AssistantPage");
const BranchesPage = page(() => import("./pages/BranchesPage"), "BranchesPage");
const ComparePage = page(() => import("./pages/ComparePage"), "ComparePage");
const ComplaintsPage = page(() => import("./pages/ComplaintsPage"), "ComplaintsPage");
const DashboardPage = page(() => import("./pages/DashboardPage"), "DashboardPage");
const DocumentDetailPage = page(() => import("./pages/DocumentDetailPage"), "DocumentDetailPage");
const DocumentViewerPage = page(() => import("./pages/DocumentViewerPage"), "DocumentViewerPage");
const DocumentsPage = page(() => import("./pages/DocumentsPage"), "DocumentsPage");
const NginxPage = page(() => import("./pages/NginxPage"), "NginxPage");
const NotificationsPage = page(() => import("./pages/NotificationsPage"), "NotificationsPage");
const OrganizationsPage = page(() => import("./pages/OrganizationsPage"), "OrganizationsPage");
const PoliciesPage = page(() => import("./pages/PoliciesPage"), "PoliciesPage");
const PolicyDetailPage = page(() => import("./pages/PolicyDetailPage"), "PolicyDetailPage");
const SupportPage = page(() => import("./pages/SupportPage"), "SupportPage");
const TicketPage = page(() => import("./pages/TicketPage"), "TicketPage");
const UploadBatchPage = page(() => import("./pages/UploadBatchPage"), "UploadBatchPage");
const UploadBatchesPage = page(() => import("./pages/UploadBatchPage"), "UploadBatchesPage");
const UploadPage = page(() => import("./pages/UploadPage"), "UploadPage");
const UsersPage = page(() => import("./pages/UsersPage"), "UsersPage");

function Guard({ permission, children }: { permission: string; children: ReactNode }) {
  const { can } = useAuth();
  return can(permission) ? children : <NotFoundPage />;
}

export function App() {
  const dispatch = useAppDispatch();
  const { me, loading, can } = useAuth();
  const restoreStarted = useRef(false);

  useEffect(() => {
    // React Strict Mode re-runs effects in development. Refresh tokens rotate
    // on use, so two restore calls would make the second call look like token reuse.
    if (loading && !restoreStarted.current) {
      restoreStarted.current = true;
      dispatch(restoreSession());
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  if (loading) {
    return <div className="flex h-screen items-center justify-center"><Spinner /></div>;
  }
  if (!me) {
    return (
      <>
        <Routes>
          <Route path="forgot-password" element={<ForgotPasswordPage />} />
          <Route path="reset-password" element={<ResetPasswordPage />} />
          <Route path="*" element={<LoginPage />} />
        </Routes>
        <Toaster />
      </>
    );
  }
  // The Master Admin runs the platform and never reads tenant knowledge, so it lands on Organizations.
  const home = can("documents:read") ? <DashboardPage /> : <Navigate to="/organizations" replace />;
  return (
    <>
      <Routes>
        {/* A reset link opened while signed in: the reset ends this session too. */}
        <Route path="reset-password" element={<ResetPasswordPage />} />
        <Route element={<AppLayout />}>
          <Route index element={home} />

          {/* AI — access-controlled retrieval, latest version first (spec §28–35) */}
          <Route path="assistant" element={<Guard permission="ai:query"><AssistantPage /></Guard>} />

          {/* Knowledge */}
          <Route path="policies" element={<Guard permission="policies:read"><PoliciesPage /></Guard>} />
          <Route path="policies/:id" element={<Guard permission="policies:read"><PolicyDetailPage /></Guard>} />
          <Route path="policies/:id/compare" element={<Guard permission="policies:read"><ComparePage /></Guard>} />
          <Route path="documents" element={<Guard permission="documents:read"><DocumentsPage /></Guard>} />
          <Route path="documents/upload" element={<Guard permission="documents:upload"><UploadPage /></Guard>} />
          <Route path="documents/:id" element={<Guard permission="documents:read"><DocumentDetailPage /></Guard>} />
          <Route path="documents/:id/view" element={<Guard permission="documents:read"><DocumentViewerPage /></Guard>} />
          <Route path="uploads" element={<Guard permission="documents:upload"><UploadBatchesPage /></Guard>} />
          <Route path="uploads/:id" element={<Guard permission="documents:upload"><UploadBatchPage /></Guard>} />

          {/* Organization */}
          <Route path="users" element={<Guard permission="users:manage">{me.role === "master_admin" || me.role === "org_admin" ? <AdminUsersPage /> : <UsersPage />}</Guard>} />
          <Route path="branches" element={<Guard permission="branches:manage"><BranchesPage /></Guard>} />
          <Route path="assignments" element={<Guard permission="policies:assign"><AssignDocumentsPage /></Guard>} />
          <Route path="organizations" element={<Guard permission="organizations:manage"><OrganizationsPage /></Guard>} />
          <Route path="organizations/:id/nginx" element={<Guard permission="organizations:manage"><NginxPage /></Guard>} />

          {/* Account */}
          <Route path="notifications" element={<NotificationsPage />} />
          <Route path="complaints" element={<Guard permission="support"><ComplaintsPage /></Guard>} />
          <Route path="support" element={<Guard permission="support"><SupportPage /></Guard>} />
          <Route path="support/:id" element={<Guard permission="support"><TicketPage /></Guard>} />

          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Routes>
      <Toaster />
    </>
  );
}
