from torch.utils.data import Dataset
from transformers import AutoTokenizer
from utils import get_users
import torch
from tqdm.auto import tqdm
from nltk import ngrams
import random
from torch.nn.utils.rnn import pad_sequence

class TestSamples(Dataset):
    '''
    This class is used for generating test samples for evaluation.
    '''
    def __init__(self, config, data, full_data):
        self.data = data
        self.users = [user for user in get_users(data) if len(user.get('articles_id', [])) >= 4]  # Filter users with 4 interactions or more
        self.tokenizer = AutoTokenizer.from_pretrained("VietAI/vit5-base")
        self.trigram_dim = config['DATA']['TRIGRAM_DIM']
        self.num_items = config['DATA']['NUM_ITEMS']  # number of articles
        self.samples = []

        # Prepare article descriptions and IDs
        full_data.sort_values(by='article_id', inplace=True)
        self.article_desc = dict(zip(full_data.article_id, full_data.description))
        self.article_tags = dict(zip(full_data.article_id, full_data.tags))  # Mapping of article_id to tags

        # Precompute user samples without tokenization or tensor conversions
        for user in tqdm(self.users):
            interacted_ids = set(user.get('articles_id', []))
            non_interacted_ids = list(set(self.article_desc.keys()) - interacted_ids)

            # Sample non-interacted articles if necessary
            sampled_non_interacted = random.sample(
                non_interacted_ids, max(0, self.num_items - len(interacted_ids))
            )
            selected_ids = list(interacted_ids) + sampled_non_interacted
            random.shuffle(selected_ids)  # Shuffle to mix interacted and non-interacted

            # Generate labels (1 for interacted, 0 for non-interacted)
            labels = [1 if article_id in interacted_ids else 0 for article_id in selected_ids]

            # Generate comments: actual comments for interacted articles, "Chưa đọc" for non-interacted
            comments = []
            for article_id in selected_ids:
                if article_id in interacted_ids:
                    user_comment = self.data.loc[(self.data.usr_id == user['usr_id']) & (self.data.article_id == article_id), 'user_comment'].values[0]
                    comments.append(str(user['usr_id']) + ": " + user_comment)  # Add usr_id to the comment
                else:
                    comments.append(str(user['usr_id']) + ": " + "Chưa đọc")

            # Generate trigrams from user comments
            user_comments = data.loc[data.usr_id == user['usr_id'], 'user_comment'].tolist()
            trigrams = [' '.join(grams) for comment in user_comments for grams in ngrams(comment.split(), 3)]

            # Retrieve article tags for selected articles
            article_tags = [self.article_tags.get(article_id, '') for article_id in selected_ids]

            # Append sample data
            self.samples.append({
                'id': user['usr_id'],
                'selected_ids': selected_ids,
                'comments': comments,  # Updated comments list
                'labels': labels,
                'usr_trigram': trigrams,
                'user_tags': [', '.join(user.get('tags', []))] * self.num_items,  # Add user tags
                'article_tags': article_tags  # Add article tags
            })

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]

    def collate_fn(self, batch):
        # Extract data from the batch
        ids = [item['id'] for item in batch]
        selected_ids = [item['selected_ids'] for item in batch]
        labels = [torch.tensor(item['labels'], dtype=torch.float) for item in batch]
        comments = [item['comments'] for item in batch]
        usr_trigrams = [item['usr_trigram'] for item in batch]
        user_tags = [item['user_tags'] for item in batch]  # Get user tags
        article_tags = [item['article_tags'] for item in batch]  # Get article tags

        # Tokenize and pad article descriptions
        article_descs = [
            [self.article_desc[article_id] for article_id in batch_item['selected_ids']]
            for batch_item in batch
        ]
        article_desc_tokenized = self.tokenizer(
            [desc for descs in article_descs for desc in descs],
            padding="max_length", max_length=150, truncation=True, return_tensors='pt'
        ).input_ids.view(len(batch), -1, 150)  # Reshape to [batch_size, num_items, max_length]

        # Tokenize user comments
        comments_tokenized = self.tokenizer(
            [comment for sublist in comments for comment in sublist],
            padding="max_length", max_length=150, truncation=True, return_tensors='pt'
        ).input_ids.view(len(batch), -1, 150)  # Reshape as above

        # Process user trigrams
        usr_trigrams_tokenized = []
        for i, trigrams in enumerate(usr_trigrams):
            if trigrams:
                trigrams_tokenized = self.tokenizer(
                    trigrams, padding="max_length", max_length=self.trigram_dim,
                    truncation=True, return_tensors='pt'
                ).input_ids
            else:
                trigrams_tokenized = torch.zeros((1, self.trigram_dim), dtype=torch.long)

            # Repeat trigrams for all items for this user
            trigrams_repeated = trigrams_tokenized.unsqueeze(0).repeat(len(selected_ids[i]), 1, 1)
            usr_trigrams_tokenized.append(trigrams_repeated)

        # Stack and pad to ensure consistent dimensions
        usr_trigrams_tokenized = pad_sequence(
            usr_trigrams_tokenized, batch_first=True, padding_value=0
        )  # Shape: [batch_size, num_items, trigram_dim]

        # Tokenize user tags
        user_tags_tokenized = self.tokenizer(
            [tag for sublist in user_tags for tag in sublist],
            padding="max_length", max_length=150, truncation=True, return_tensors='pt'
        ).input_ids.view(len(batch), -1, 150)  

        # Tokenize article tags
        article_tags_tokenized = self.tokenizer(
            [tag for sublist in article_tags for tag in sublist],
            padding="max_length", max_length=150, truncation=True, return_tensors='pt'
        ).input_ids.view(len(batch), -1, 150)

        return {
            'id': ids,
            'article_ids': selected_ids,
            'usr_comments': comments_tokenized,
            'descriptions': article_desc_tokenized,
            'labels': pad_sequence(labels, batch_first=True, padding_value=0),
            'usr_trigram': usr_trigrams_tokenized,
            'usr_tags': user_tags_tokenized,  # Return user tags
            'article_tags': article_tags_tokenized  # Return article tags
        }