import CameraMap from "@/components/CameraMap";
import Sidebar from "@/components/Sidebar";

export default function Home() {
  return (
    <div className="flex flex-1 flex-col">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-4 py-3 dark:border-slate-800 dark:bg-slate-900">
        <h1 className="text-lg font-semibold text-slate-900 dark:text-slate-50">
          PRAHARI
        </h1>
        <span className="text-xs text-slate-500 dark:text-slate-400">
          Gujarat Sentinel — camera health
        </span>
      </header>
      <div className="flex flex-1 overflow-hidden">
        <main className="flex-1">
          <CameraMap />
        </main>
        <Sidebar />
      </div>
    </div>
  );
}
