"""Telegram notification and authenticated per-result approval callbacks."""
import requests


class Telegram:
    def __init__(self, token, chat_id, reviewer_id=None):
        self.token = token
        self.chat_id = str(chat_id)
        self.reviewer_id = str(reviewer_id or chat_id)
        if not token:
            raise ValueError('Chưa lưu Bot token Telegram. Điền token rồi bấm Lưu cấu hình.')
        if not self.chat_id.lstrip('-').isdigit():
            raise ValueError('Thiếu Chat ID Telegram hoặc ID không hợp lệ. Mở cuộc chat với bot, gửi /start rồi điền Chat ID nhận thông báo.')
        if not self.reviewer_id.isdigit():
            raise ValueError('Cần User ID người duyệt hợp lệ. Với chat riêng, bạn có thể để trống ô User ID.')

    def call(self, method, data=None, files=None):
        # Never expose the token-bearing URL through HTTP exception messages/logs.
        try:
            response = requests.post(f'https://api.telegram.org/bot{self.token}/{method}',
                data=data or {}, files=files, timeout=(10, 45 if files else 20))
            obj = response.json()
        except (requests.RequestException, ValueError):
            raise RuntimeError(f'Telegram {method} could not confirm delivery; check the bot before retrying') from None
        if not obj.get('ok'):
            raise RuntimeError('Telegram: ' + str(obj.get('description', 'Request failed')))
        return obj.get('result')

    def notify(self, text, approve_data, stop_data, preview=None):
        import json
        markup = json.dumps({'inline_keyboard': [[{'text': 'Approve → kịch bản tiếp', 'callback_data': approve_data},
                                                  {'text': 'Dừng', 'callback_data': stop_data}]]})
        data = {'chat_id': self.chat_id, 'reply_markup': markup}
        if preview and preview.is_file():
            data.update(caption=text[:1024], supports_streaming='true')
            with preview.open('rb') as file:
                return self.call('sendVideo', data, {'video': file})
        data['text'] = text[:4000]
        return self.call('sendMessage', data)

    def check(self):
        self.call('getMe')
        self.call('getChat', {'chat_id': self.chat_id})
        if self.call('getWebhookInfo').get('url'):
            raise ValueError('Telegram bot đang dùng webhook khác. Chọn bot riêng cho hàng đợi; tool không tự xóa webhook hiện có.')

    def allowed_callback(self, callback, expected_message_id):
        message = callback.get('message', {})
        return (str(message.get('chat', {}).get('id')) == self.chat_id and
                str(callback.get('from', {}).get('id')) == self.reviewer_id and
                message.get('message_id') == expected_message_id)

    def updates(self, offset):
        import json
        return self.call('getUpdates', {'offset': offset, 'timeout': 5,
                                       'allowed_updates': json.dumps(['callback_query'])})

    def answer(self, callback_id, text):
        return self.call('answerCallbackQuery', {'callback_query_id': callback_id, 'text': text[:180]})

    def remove_buttons(self, message_id):
        return self.call('editMessageReplyMarkup', {'chat_id': self.chat_id, 'message_id': message_id,
                                                   'reply_markup': '{"inline_keyboard":[]}'})
