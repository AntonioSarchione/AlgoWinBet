import { Sidebar } from "@/app/_components/Sidebar";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="shell">
      <Sidebar />
      <main className="main" id="contenuto">
        {children}
      </main>
    </div>
  );
}
