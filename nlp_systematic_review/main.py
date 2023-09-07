from bertopic import *
import pandas as pd
import numpy as np

from nlp_systematic_review.data import *#load_data_bq, get_data_from_bq, get_data_row_count
from nlp_systematic_review.params import *

from bertopic.representation import KeyBERTInspired
from sklearn.feature_extraction.text import CountVectorizer, ENGLISH_STOP_WORDS
from gensim.corpora.dictionary import Dictionary
from gensim.models.coherencemodel import CoherenceModel
from gensim.models.ldamodel import LdaModel


def get_raw_data():
    print('function start')
    table = f"raw_{TABLE}"
    query = f"""
        SELECT
          *
        FROM {GCP_PROJECT_SEBT84}.{BQ_DATASET}.{table}
        """
    df = get_data_from_bq(query)

    print(f"✅ Raw data extracted from bigquery, with shape {df.shape}")

    return df

def preprocess_data_bq(df: pd.DataFrame):
    """provide the DataFrame it will preprocessed the dataset and upload it to BQ"""

    assert isinstance(df, pd.DataFrame), "path_to_csv should be a string"

    df = df.sort_values(by=['abstract_id', 'line_number'])
    df['abstract_text'] = df['abstract_text'].astype(str)
    concatenated_abstract = df.groupby('abstract_id')['abstract_text'].apply(' '.join).reset_index()
    df = df.merge(concatenated_abstract, on='abstract_id', how='left')
    data = df[['abstract_id', 'abstract_text_y']].drop_duplicates().rename(columns={'abstract_text_y': 'abstract_text'})

    load_data_bq(data,replace=True)

    print(f"✅ Data processed and uploaded to bigquery, with shape {data.shape}")

def get_processed_data(frac=0.02
                       ,table='concat_pubmed'):

    """get processed data to feed model, you can select the size of the subset needed"""
    table_size = get_data_row_count(table)

    sample_size = round(table_size * frac)

    query = f"""
        SELECT
          *
        FROM {GCP_PROJECT_SEBT84}.{BQ_DATASET}.{table}
        limit {sample_size}
        """

    df = get_data_from_bq(query)

    return df

def preprocess_data(path_to_csv, frac):
    """provide the path to csv and the frac of data you want to use and it will
    return the preprocessed dataset in pd.DataFrame format"""

    assert isinstance(path_to_csv, str), "path_to_csv should be a string"
    df = pd.read_csv(path_to_csv)
    df = df.sort_values(by=['abstract_id', 'line_number'])
    df['abstract_text'] = df['abstract_text'].astype(str)
    concatenated_abstract = df.groupby('abstract_id')['abstract_text'].apply(' '.join).reset_index()
    df = df.merge(concatenated_abstract, on='abstract_id', how='left')
    data = df[['abstract_id', 'abstract_text_y']].drop_duplicates().rename(columns={'abstract_text_y': 'abstract_text'})
    data = data.sample(frac=frac).reset_index(drop=True)

    return data

def train_model(processed_df, use_stop_words=False):
    """Input a df, train the model with an option to use custom stop words."""

    if use_stop_words:

        documents = processed_df['abstract_text'].tolist()

        vectorizer = CountVectorizer(stop_words=list(ENGLISH_STOP_WORDS))
        X = vectorizer.fit_transform(documents).toarray()
        terms = vectorizer.get_feature_names_out()

        document_lengths = X.sum(axis=1)
        normalized_tf = X / document_lengths[:, None]

        sum_normalized_tf = normalized_tf.sum(axis=0)
        tf_df = pd.DataFrame({'term': terms, 'normalized_tf': sum_normalized_tf})
        sorted_tf_df = tf_df.sort_values(by='normalized_tf', ascending=False)

        N = 20
        top_N_terms = sorted_tf_df.head(N)['term'].tolist()

        X_percentage = 0.85
        term_document_counts = (X > 0).sum(axis=0)
        terms_to_remove = [term for term, count in zip(terms, term_document_counts) if count / len(documents) > X_percentage]

        custom_stop_words = set(top_N_terms).union(set(terms_to_remove))

        vectorizer_model = CountVectorizer(ngram_range=(1, 1), stop_words=list(custom_stop_words))
        representation_model = KeyBERTInspired()
        topic_model = BERTopic(vectorizer_model=vectorizer_model, representation_model=representation_model)

    else:
        topic_model = BERTopic()

    topics, probs = topic_model.fit_transform(processed_df['abstract_text'])

    return topics, probs, topic_model

def get_topic_infos(topic_model):
    """returns a df with Topic, Count, Name, Representation, Representative_Docs"""
    topic_info = topic_model.get_topic_info()

    return topic_info


def coherence_metric(topic_model, data):
    """
    Calculate coherence score for topics generated by BERTopic.

    Parameters:
    - topic_model: Trained BERTopic model
    - df: DataFrame containing the text data

    Returns:
    - coherence: Average topic coherence score
    """

    topics = topic_model.get_topics()


    texts = [text.split() for text in data['abstract_text']]
    dictionary = Dictionary(texts)
    corpus = [dictionary.doc2bow(text) for text in texts]


    gensim_topics = {key: [word[0] for word in value] for key, value in topics.items()}
    lda_topics = list(gensim_topics.values())


    cm = CoherenceModel(topics=lda_topics, texts=texts, dictionary=dictionary, coherence='c_v')
    coherence = cm.get_coherence()

    return coherence

def topic_diversity(topic_model, top_n=10):
    """
    Calculate the diversity of topics generated by the model.

    This is done by computing the pairwise Jaccard similarity between the top_n words of each topic.
    The diversity is then 1 minus the average of these Jaccard similarities. Thus, a higher score
    indicates more diverse topics.

    Parameters:
    - topic_model: A trained BERTopic model.
    - top_n: The number of top words/terms to consider for each topic when calculating diversity.

    Returns:
    - diversity: A float representing the diversity of topics.
    """

    # Extract words associated with each topic from the model
    topic_words = topic_model.get_topic_info()

    # Prepare a list of sets of top words for each topic
    topics_terms = []
    for _, group in topic_words.groupby('Topic'):
        # Here we take the first 'top_n' words for each topic from the 'Representation' column
        terms = set(group['Representation'].iloc[0][:top_n])
        topics_terms.append(terms)

    # Calculate pairwise Jaccard similarities for the sets of top words
    jaccard_similarities = []
    for i in range(len(topics_terms)):
        for j in range(i+1, len(topics_terms)):
            intersection_len = len(topics_terms[i].intersection(topics_terms[j]))
            union_len = len(topics_terms[i].union(topics_terms[j]))
            jaccard_similarity = intersection_len / union_len
            jaccard_similarities.append(jaccard_similarity)

    # The diversity score is 1 minus the average Jaccard similarity
    diversity = 1 - sum(jaccard_similarities) / len(jaccard_similarities)

    return diversity


def visualize_data_v(processed_df, visu_type, html):
    """
    Visualizes topics using BERTopic. visu_type: Type of visualization ('circle', 'bar', or 'rank'),
    use html = True when working outside of Google Colab.
    """
    representation_model = KeyBERTInspired()
    topic_model = BERTopic(representation_model=representation_model)
    topics, probs = topic_model.fit_transform(processed_df['abstract_text'])
    if html:
        if visu_type == 'circle':
            circle = topic_model.visualize_topics()
            circle.write_html("circle.html")

        elif visu_type == 'bar':
            bar = topic_model.visualize_barchart()
            bar.write_html("bar.html")

        elif visu_type == 'rank':
            rank = topic_model.visualize_term_rank()
            rank.write_html("rank.html")
    else:
        if visu_type == 'circle':
            circle = topic_model.visualize_topics()

            return circle

        elif visu_type == 'bar':
            bar = topic_model.visualize_barchart()

            return bar

        elif visu_type == 'rank':
            rank = topic_model.visualize_term_rank()

            return rank



def get_topics_kw(topic_model):
    """returns a df with 10 main keywords for each topic"""

    topics = topic_model.get_topics()
    topic_df = pd.DataFrame({topic_id: [word for word, _ in words] for topic_id, words in topics.items()})
    col_rename = {topic_id: f"{topic_id}" for topic_id in topic_df.columns}
    topic_df.rename(columns=col_rename, inplace=True)

    return topic_df

def get_id_prob_key(topic_model, method, processed_df):
    """Returns a df with the doc_id, the text content, the topic and topic name,
    and the prob of the doc belonging to this topic.
    Methods available: 'main_name' for top word, '10_kw' for 10 keywords with
    respect to the related topic."""

    topics = topic_model.get_topics()
    topic_df = pd.DataFrame({topic_id: [word for word, _ in words] for topic_id, words in topics.items()})
    col_rename = {topic_id: f"{topic_id}" for topic_id in topic_df.columns}
    topic_df.rename(columns=col_rename, inplace=True)
    document = topic_model.get_document_info(processed_df['abstract_text'])
    document_and_proba = document[['Document', 'Topic', 'Probability']]

    if method == 'main_name':
        document_and_proba['Topic_name'] = document_and_proba['Topic'].apply(lambda topic_id: topics.get(int(topic_id), [])[0][0]) #for top word (topic main name)
        doc_with_abstract_id_prob_and_topic_name = document_and_proba.set_index(processed_df.index)

        return doc_with_abstract_id_prob_and_topic_name

    elif method == '10_kw':
        document_and_proba['Topic_name'] = document_and_proba['Topic'].apply(lambda topic_id: ', '.join([word for word, _ in topics.get(int(topic_id), [])])) #for all words with respect to the topic
        doc_with_abstract_id_prob_and_topic_name = document_and_proba.set_index(processed_df.index)

        return doc_with_abstract_id_prob_and_topic_name



def find_article(query,model,path_to_csv, frac):
    # setup an URL concate generator
    def url_destination(id):
        #ULR example: 'https://pubmed.ncbi.nlm.nih.gov/16364933/'
        url_template = 'https://pubmed.ncbi.nlm.nih.gov/'
        return f"{url_template}{id}"

    data = preprocess_data(path_to_csv, frac)

    df_with_topics = pd.concat([data['abstract_id'],model.get_document_info(data)],axis=1)

    # Find topics from query
    f_topics, f_prob = model.find_topics(query)
    topic_info = model.get_topic_info()

    # extarct the options from the DB
    for t in range(len(f_topics)):
        topic_id = f_topics[t]
        topic_prob = round(f_prob[t]*100,2)
        topic_name = topic_info['Name'][topic_info['Topic'] == topic_id].values[0]
        article_count = df_with_topics['abstract_id'][df_with_topics['Topic'] == t].count()
        print(f"Recommended Topics: {topic_name} with a probability of {topic_prob}% & we've found {article_count} articles\n")

    # Ask user for topic selection
    selected_id = input('select a topic ID to show the articles:')

    # Generate the article destination URL + display the options
    article_list = df_with_topics[df_with_topics['Topic'] == int(selected_id)]#.count()
    article_list['article_link'] = article_list['abstract_id'].apply(url_destination)
    article_list[['Document','article_link']]

def save_model(topic_model, path):
    """save model into a given path"""
    assert isinstance(path, str), 'please provide a string as path'
    topic_model.save(path, serialization="safetensors")

def load_model():
    """load model from a directory path. the directory should contain json files
    and safetensor file"""
    path = ''
    loaded_model = BERTopic.load(path)
    return loaded_model
