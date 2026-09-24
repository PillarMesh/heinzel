import {Component, type ReactNode} from "react"

import {RecoveryPage} from "../routes/recovery-page"

interface ErrorBoundaryProps {
  readonly children: ReactNode
}

interface ErrorBoundaryState {
  readonly failed: boolean
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = {failed: false}

  static getDerivedStateFromError(): ErrorBoundaryState {
    return {failed: true}
  }

  componentDidCatch(): void {
    // React owns development diagnostics. The product view never repeats an untrusted exception.
  }

  render(): ReactNode {
    if (this.state.failed) {
      return <RecoveryPage kind="view" />
    }
    return this.props.children
  }
}
