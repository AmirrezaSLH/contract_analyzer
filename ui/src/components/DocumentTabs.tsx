import { NavLink } from "react-router-dom";
import styles from "../App.module.css";

/**
 * Analysis and Chat: two views *of the selected document*.
 *
 * They render only on the two routes that have one. Upload and the library are
 * application pages, not views of a document, and showing "Analysis | Chat"
 * there would offer two tabs that do not apply to what is on screen.
 */
export function DocumentTabs({ documentId }: { documentId: number }) {
  return (
    <div className={styles.tabs} role="tablist" aria-label="Views of this document">
      {TABS.map((tab) => (
        <NavLink
          key={tab.path}
          role="tab"
          to={`/documents/${documentId}/${tab.path}`}
          className={({ isActive }) => `${styles.tab} ${isActive ? styles.tabActive : ""}`}
        >
          {tab.label}
        </NavLink>
      ))}
    </div>
  );
}

const TABS = [
  { path: "analysis", label: "Analysis" },
  { path: "chat", label: "Chat" },
] as const;
