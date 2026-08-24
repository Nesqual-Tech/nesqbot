import { useRouter } from "expo-router"
import { EmptyState, Screen } from "../src/components"

export default function NotFoundScreen(): JSX.Element {
  const router = useRouter()
  return (
    <Screen contentContainerStyle={{ justifyContent: "center" }}>
      <EmptyState
        title="That screen does not exist"
        glyph="?"
        message="The link may be out of date, or the notification pointed at something that has since been removed."
        actionLabel="Go home"
        onAction={() => router.replace("/")}
      />
    </Screen>
  )
}
