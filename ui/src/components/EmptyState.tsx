import type { ReactNode } from "react";
import { Card } from "./Card";
import { Icon, type IconName } from "./Icon";
import styles from "./EmptyState.module.css";

interface Props {
  icon?: IconName;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
}

export function EmptyState({ icon = "document-lines", title, body, action }: Props) {
  return (
    <Card large className={styles.empty}>
      <Icon name={icon} size={30} className={styles.icon} />
      <div className={styles.title}>{title}</div>
      {body ? <div className={styles.body}>{body}</div> : null}
      {action ? <div className={styles.action}>{action}</div> : null}
    </Card>
  );
}
