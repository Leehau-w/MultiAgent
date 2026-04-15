import { useToastStore } from '../stores/toastStore'

const ICON: Record<string, string> = {
  success: '\u2713',
  error: '\u2717',
  info: '\u2139',
}

const BG: Record<string, string> = {
  success: 'bg-green-600/90',
  error: 'bg-red-600/90',
  info: 'bg-blue-600/90',
}

export default function ToastContainer() {
  const { toasts, remove } = useToastStore()

  if (toasts.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 max-w-sm">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`${BG[t.type]} text-white text-sm px-4 py-2.5 rounded-lg shadow-lg flex items-start gap-2 animate-slide-up`}
        >
          <span className="shrink-0 mt-0.5">{ICON[t.type]}</span>
          <span className="flex-1 break-words">{t.message}</span>
          <button
            onClick={() => remove(t.id)}
            className="shrink-0 text-white/60 hover:text-white ml-1"
          >
            &times;
          </button>
        </div>
      ))}
    </div>
  )
}
