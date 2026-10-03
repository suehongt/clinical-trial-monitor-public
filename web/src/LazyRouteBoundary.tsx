import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, InlineAlert, Skeleton } from "./ui";
import type { Translate } from "./i18n";

export function RouteLoading({ t }: { t: Translate }) {
  return <section className="page-main route-loading" aria-label={t("route.loading")} aria-busy="true">
    <div className="page-head"><div><div className="route-title-placeholder" /></div></div>
    <Skeleton label={t("route.loading")} lines={5} />
  </section>;
}

export class LazyRouteBoundary extends Component<{ children: ReactNode; t: Translate }, { error: boolean }> {
  state = { error: false };
  static getDerivedStateFromError() { return { error: true }; }
  componentDidCatch(error: Error, info: ErrorInfo) {
    if (import.meta.env.DEV) console.error("Route chunk failed", error, info.componentStack);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return <section className="page-main"><InlineAlert tone="error" title={this.props.t("route.loadFailed")}
      action={<Button variant="secondary" onClick={() => location.reload()}>{this.props.t("common.retry")}</Button>}>
      {this.props.t("route.loadFailedHint")}
    </InlineAlert></section>;
  }
}
