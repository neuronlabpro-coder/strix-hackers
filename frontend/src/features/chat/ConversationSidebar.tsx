import { MessageSquarePlus, Trash2 } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type { ChatConversation } from '../../types/api'
import { relativeDate } from './relativeDate'

/**
 * Las conversaciones del workspace, con su borrador y su borrado.
 */

export interface ConversationSidebarProps {
  conversations: ChatConversation[]
  activeId: string | null
  isLoading: boolean
  loadFailed: boolean
  total: number
  onSelect: (conversationId: string) => void
  onNew: () => void
  onRemove: (conversationId: string) => void
  onRetry: () => void
}

export function ConversationSidebar({
  conversations,
  activeId,
  isLoading,
  loadFailed,
  total,
  onSelect,
  onNew,
  onRemove,
  onRetry,
}: ConversationSidebarProps) {
  const { t, i18n } = useTranslation('chat')

  return (
    <aside className="chat-sidebar" aria-label={t('sidebar.label')}>
      <button className="primary-button chat-new-button" type="button" onClick={onNew}>
        <MessageSquarePlus size={16} aria-hidden="true" />
        {t('sidebar.new')}
      </button>

      <div className="chat-sidebar-heading">
        <span>{t('sidebar.heading')}</span>
        {/* El total se muestra solo si el listado esta completo. Con paginacion, `total`
            contaria paginas que el panel no ha pedido, y un "de 200" sobre una pantalla con
            veinte filas hace pensar que faltan mil y no se han cargado. */}
        {!isLoading && !loadFailed && total <= conversations.length && total > 0 && (
          <span className="chat-sidebar-count">{total}</span>
        )}
      </div>

      {isLoading && <p className="chat-sidebar-note">{t('sidebar.loading')}</p>}

      {loadFailed && (
        <div className="chat-sidebar-note chat-sidebar-note-error">
          <p>{t('sidebar.loadFailed')}</p>
          <button className="secondary-button" type="button" onClick={onRetry}>
            {t('sidebar.retry')}
          </button>
        </div>
      )}

      {!isLoading && !loadFailed && conversations.length === 0 && (
        <p className="chat-sidebar-note">{t('sidebar.empty')}</p>
      )}

      <ul className="chat-conversation-list">
        {conversations.map((conversacion) => {
          const estaActiva = conversacion.id === activeId
          return (
            <li key={conversacion.id}>
              <div className={estaActiva ? 'chat-conversation chat-conversation-active' : 'chat-conversation'}>
                <button
                  className="chat-conversation-open"
                  type="button"
                  onClick={() => onSelect(conversacion.id)}
                  aria-current={estaActiva ? 'true' : undefined}
                >
                  <span className="chat-conversation-title">{conversacion.title}</span>
                  <span className="chat-conversation-meta">
                    {relativeDate(conversacion.updated_at, i18n.language)}
                    {conversacion.message_count > 0 && (
                      <>
                        {' · '}
                        {t('sidebar.messageCount', { count: conversacion.message_count })}
                      </>
                    )}
                  </span>
                </button>
                {/* El borrado es un boton hermano y no esta dentro del de abrir. Meterlo
                    dentro haria que al pulsarlo se abriera el hilo, que es lo contrario de lo
                    que el usuario quiere, y ademas un boton dentro de un boton es HTML invalido
                    y no navegable con teclado. */}
                <button
                  className="chat-conversation-delete"
                  type="button"
                  onClick={() => onRemove(conversacion.id)}
                  aria-label={t('sidebar.deleteAria', { title: conversacion.title })}
                >
                  <Trash2 size={14} aria-hidden="true" />
                </button>
              </div>
            </li>
          )
        })}
      </ul>
    </aside>
  )
}
